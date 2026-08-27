import asyncio
import json
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

import httpx
import redis
from fastapi import HTTPException

import app as api


def price(index: int) -> dict:
    return {
        "crop": f"Crop {index}",
        "min_price": float(index),
        "max_price": float(index + 2),
        "average_price": float(index + 1),
    }


def weather(name: str) -> dict:
    return {
        "governorate": name,
        "temp": 30.0,
        "feels_like": 31.0,
        "humidity": 50,
        "pressure": 1010,
        "description": "clear sky",
        "main": "Clear",
        "wind_speed": 2.5,
        "visibility": 10000,
    }


class DummyHttpClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class ResultPipeline:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.commands = []

    def exists(self, *args):
        self.commands.append(("exists", args))
        return self

    def llen(self, *args):
        self.commands.append(("llen", args))
        return self

    def lrange(self, *args):
        self.commands.append(("lrange", args))
        return self

    def get(self, *args):
        self.commands.append(("get", args))
        return self

    def delete(self, *args):
        self.commands.append(("delete", args))
        return self

    def rpush(self, *args):
        self.commands.append(("rpush", args))
        return self

    def set(self, *args):
        self.commands.append(("set", args))
        return self

    def execute(self):
        self.commands.append(("execute", ()))
        if self.error:
            raise self.error
        return self.result


class PipelineRedis:
    def __init__(self, pipeline):
        self.pipeline_instance = pipeline
        self.transactions = []

    def pipeline(self, transaction):
        self.transactions.append(transaction)
        return self.pipeline_instance


class PriceParsingTests(unittest.TestCase):
    def test_parse_price_page_returns_rows_and_relevant_pagination_links(self):
        html = """
        <table><tbody><tr>
          <td>Wheat</td><td>١٠</td><td>20</td><td>15</td>
        </tr></tbody></table>
        <a href="?page=2">2</a>
        <a href="/market-price/price-list?page=3">3</a>
        <a href="/unrelated?page=99">ignore</a>
        """

        rows, pages = api.parse_price_page(html)

        self.assertEqual(rows, [{
            "crop": "Wheat",
            "min_price": 10.0,
            "max_price": 20.0,
            "average_price": 15.0,
        }])
        self.assertEqual(pages, {2, 3})


class RetryTests(unittest.TestCase):
    def test_transient_errors_are_retried(self):
        request_count = 0

        def handler(request):
            nonlocal request_count
            request_count += 1
            return httpx.Response(503 if request_count < 3 else 200, request=request)

        with (
            httpx.Client(transport=httpx.MockTransport(handler)) as client,
            patch.object(api, "HTTP_MAX_ATTEMPTS", 3),
            patch.object(api, "HTTP_RETRY_BACKOFF_SECONDS", 0),
            patch.object(api.time, "sleep") as sleep,
            patch.object(api.logger, "warning"),
        ):
            response = api._request_with_retries(client, "https://example.test", resource="test")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_non_retryable_client_error_fails_immediately(self):
        request_count = 0

        def handler(request):
            nonlocal request_count
            request_count += 1
            return httpx.Response(401, request=request)

        with (
            httpx.Client(transport=httpx.MockTransport(handler)) as client,
            patch.object(api, "HTTP_MAX_ATTEMPTS", 3),
            self.assertRaises(api.ScrapeIncompleteError),
        ):
            api._request_with_retries(client, "https://example.test", resource="test")

        self.assertEqual(request_count, 1)


class ConcurrentScrapingTests(unittest.TestCase):
    def test_price_pages_are_crawled_and_returned_in_page_order(self):
        pages = {
            1: ([price(1)], {1, 2, 3}),
            2: ([price(2)], {1, 2, 3}),
            3: ([price(3)], {1, 2, 3}),
        }
        barrier = threading.Barrier(2)

        def fake_fetch(client, page_number):
            if page_number > 1:
                barrier.wait(timeout=2)
            return pages[page_number]

        with (
            patch.object(api.httpx, "Client", DummyHttpClient),
            patch.object(api, "_fetch_price_page", new=fake_fetch),
        ):
            result = api.fetch_all_prices()

        self.assertEqual([item["crop"] for item in result], ["Crop 1", "Crop 2", "Crop 3"])

    def test_duplicate_price_page_rejects_the_refresh(self):
        duplicate_rows = [price(1)]
        pages = {
            1: (duplicate_rows, {1, 2}),
            2: (duplicate_rows, {1, 2}),
        }

        with (
            patch.object(api.httpx, "Client", DummyHttpClient),
            patch.object(api, "_fetch_price_page", side_effect=lambda client, page_number: pages[page_number]),
            self.assertRaises(api.ScrapeIncompleteError),
        ):
            api.fetch_all_prices()

    def test_weather_requests_run_concurrently_and_preserve_configuration_order(self):
        governorates = [
            ("First", 1.0, 1.0),
            ("Second", 2.0, 2.0),
            ("Third", 3.0, 3.0),
        ]
        barrier = threading.Barrier(len(governorates))

        def fake_fetch(client, name, lat, lon):
            barrier.wait(timeout=2)
            return weather(name)

        with (
            patch.object(api, "EGYPT_GOVERNORATES", governorates),
            patch.object(api, "OPENWEATHER_API_KEY", "test-key"),
            patch.object(api, "SCRAPE_MAX_WORKERS", 3),
            patch.object(api.httpx, "Client", DummyHttpClient),
            patch.object(api, "fetch_weather_for_governorate", new=fake_fetch),
        ):
            result = api.fetch_all_weather()

        self.assertEqual([item["governorate"] for item in result], ["First", "Second", "Third"])

    def test_incomplete_weather_result_is_rejected(self):
        governorates = [("First", 1.0, 1.0), ("Second", 2.0, 2.0)]

        def fake_fetch(client, name, lat, lon):
            return weather(name) if name == "First" else None

        with (
            patch.object(api, "EGYPT_GOVERNORATES", governorates),
            patch.object(api, "OPENWEATHER_API_KEY", "test-key"),
            patch.object(api.httpx, "Client", DummyHttpClient),
            patch.object(api, "fetch_weather_for_governorate", new=fake_fetch),
            self.assertRaises(api.ScrapeIncompleteError),
        ):
            api.fetch_all_weather()


class RedisPaginationTests(unittest.TestCase):
    def test_get_prices_reads_only_the_requested_list_range(self):
        page_items = [json.dumps(price(index)) for index in range(10, 20)]
        pipeline = ResultPipeline(
            [1, 25, page_items, None, "2026-08-27T10:00:00+00:00"]
        )

        with patch.object(api, "redis_client", PipelineRedis(pipeline)):
            response = api.get_prices(page=2, page_size=10)

        self.assertEqual(response.prices[0].crop, "Crop 10")
        self.assertEqual(response.pagination.total_items, 25)
        self.assertEqual(response.pagination.total_pages, 3)
        self.assertIn(("lrange", (api.REDIS_PRICES_ITEMS_KEY, 10, 19)), pipeline.commands)

    def test_legacy_json_cache_is_supported_during_migration(self):
        legacy_items = [price(index) for index in range(25)]
        pipeline = ResultPipeline(
            [0, 0, [], json.dumps(legacy_items), "2026-08-27T10:00:00+00:00"]
        )

        with patch.object(api, "redis_client", PipelineRedis(pipeline)):
            response = api.get_prices(page=3, page_size=10)

        self.assertEqual(len(response.prices), 5)
        self.assertEqual(response.prices[0].crop, "Crop 20")
        self.assertEqual(response.pagination.total_pages, 3)

    def test_redis_outage_returns_sanitized_503(self):
        pipeline = ResultPipeline(error=redis.ConnectionError("secret internal details"))

        with (
            patch.object(api, "redis_client", PipelineRedis(pipeline)),
            patch.object(api.logger, "exception"),
        ):
            with self.assertRaises(HTTPException) as raised:
                api.get_prices()

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "Data cache is temporarily unavailable.")
        self.assertEqual(raised.exception.headers, {"Retry-After": "30"})
        self.assertNotIn("secret", raised.exception.detail)

    def test_invalid_cached_item_returns_500(self):
        pipeline = ResultPipeline([1, 1, ["not-json"], None, None])

        with (
            patch.object(api, "redis_client", PipelineRedis(pipeline)),
            patch.object(api.logger, "exception"),
        ):
            with self.assertRaises(HTTPException) as raised:
                api.get_prices()

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "Cached data is invalid.")


class RedisWriteAndLockTests(unittest.TestCase):
    def test_store_collection_uses_one_transaction_and_removes_legacy_blob(self):
        pipeline = ResultPipeline([])
        fake_redis = PipelineRedis(pipeline)

        with patch.object(api, "redis_client", fake_redis):
            timestamp = api.store_collection("items", "legacy", "updated", [price(1), price(2)])

        self.assertTrue(timestamp.endswith("+00:00"))
        self.assertEqual(fake_redis.transactions, [True])
        self.assertEqual(pipeline.commands[0], ("delete", ("items",)))
        self.assertEqual(pipeline.commands[-2], ("delete", ("legacy",)))
        self.assertEqual(pipeline.commands[-1], ("execute", ()))
        self.assertEqual(pipeline.commands[1][0], "rpush")
        self.assertEqual(len(pipeline.commands[1][1]), 3)

    def test_refresh_lock_is_non_blocking_and_released(self):
        fake_lock = Mock()
        fake_lock.acquire.return_value = True
        fake_redis = Mock()
        fake_redis.lock.return_value = fake_lock

        with patch.object(api, "redis_client", fake_redis):
            with api.refresh_lock("test-lock") as acquired:
                self.assertTrue(acquired)

        fake_lock.acquire.assert_called_once_with(blocking=False)
        fake_lock.release.assert_called_once_with()

    def test_incomplete_price_refresh_does_not_replace_cache(self):
        @contextmanager
        def acquired_lock(lock_key):
            yield True

        with (
            patch.object(api, "refresh_lock", new=acquired_lock),
            patch.object(api, "fetch_all_prices", side_effect=api.ScrapeIncompleteError("partial")),
            patch.object(api, "store_collection") as store,
            patch.object(api.logger, "warning"),
        ):
            result = api.save_prices_to_redis()

        self.assertFalse(result)
        store.assert_not_called()


class SchedulerAndOpenApiTests(unittest.TestCase):
    def test_lifespan_queues_initial_jobs_without_running_them_inline(self):
        scheduler = Mock()

        async def exercise_lifespan():
            with patch.object(api, "BackgroundScheduler", return_value=scheduler):
                async with api.lifespan(api.app):
                    self.assertTrue(scheduler.start.called)

        asyncio.run(exercise_lifespan())

        self.assertEqual(scheduler.add_job.call_count, 2)
        self.assertEqual(scheduler.shutdown.call_args.kwargs, {"wait": False})
        for call in scheduler.add_job.call_args_list:
            self.assertIsNotNone(call.kwargs["next_run_time"])
            self.assertEqual(call.kwargs["max_instances"], 1)

    def test_openapi_documents_paginated_and_background_status_codes(self):
        schema = api.app.openapi()
        for path in ("/api/v1/scraper/prices", "/api/v1/scraper/weather"):
            get_responses = schema["paths"][path]["get"]["responses"]
            self.assertTrue({"200", "422", "500", "503"}.issubset(get_responses))
            post_responses = schema["paths"][path]["post"]["responses"]
            self.assertIn("202", post_responses)
            self.assertNotIn("200", post_responses)


if __name__ == "__main__":
    unittest.main()
