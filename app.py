import os
import json
import math
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Annotated, Iterator, cast
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup
import redis
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler #changing BlockingScheduler

# ===== KEKA =====
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError
# ===== KEKA =====


load_dotenv()

logger = logging.getLogger(__name__)

# --- Environment Variables ---
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") 
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
REDIS_CONNECT_TIMEOUT_SECONDS = float(os.getenv("REDIS_CONNECT_TIMEOUT_SECONDS", "3"))
REDIS_SOCKET_TIMEOUT_SECONDS = float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "5"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
HTTP_MAX_ATTEMPTS = max(1, int(os.getenv("HTTP_MAX_ATTEMPTS", "3")))
HTTP_RETRY_BACKOFF_SECONDS = max(0.0, float(os.getenv("HTTP_RETRY_BACKOFF_SECONDS", "0.5")))
SCRAPE_MAX_WORKERS = max(1, int(os.getenv("SCRAPE_MAX_WORKERS", "5")))
SCRAPE_LOCK_TIMEOUT_SECONDS = max(60, int(os.getenv("SCRAPE_LOCK_TIMEOUT_SECONDS", "1800")))


# ===== KEKA =====
# --- Global Redis Client ---
redis_client: redis.Redis = redis.Redis(
    host=REDIS_HOST, 
    password=REDIS_PASSWORD, 
    port=REDIS_PORT, 
    decode_responses=True,
    socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
    socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    health_check_interval=30,
)
# ===== KEKA =====



# --- Constants ---

# ---- Prices ----
REDIS_PRICES_KEY = "bashaier:prices"
REDIS_PRICES_ITEMS_KEY = "bashaier:prices:items:v2"
REDIS_PRICES_LAST_UPDATED_KEY = "bashaier:prices:last_updated"
REDIS_PRICES_REFRESH_LOCK_KEY = "bashaier:prices:refresh:lock"
PRICE_LIST_BASE = "https://www.bashaier.net/market-price/price-list"
PRICE_VALUE_CHAIN_ID = 1
MAX_SOURCE_PAGES = 50

# ---- Weather ----
REDIS_WEATHER_KEY = "egypt:weather"
REDIS_WEATHER_ITEMS_KEY = "egypt:weather:items:v2"
REDIS_WEATHER_LAST_UPDATED_KEY = "egypt:weather:last_updated"
REDIS_WEATHER_REFRESH_LOCK_KEY = "egypt:weather:refresh:lock"
OPENWEATHER_BASE = "https://api.openweathermap.org/data/2.5/weather"
EGYPT_GOVERNORATES = [
    ("Cairo", 30.0444, 31.2357), ("Alexandria", 31.2001, 29.9187), ("Giza", 30.0131, 31.2089),
    ("Qalyubia", 30.1792, 31.2056), ("Dakahlia", 31.0409, 31.3785), ("Beheira", 31.0375, 30.4698),
    ("Kafr El Sheikh", 31.1117, 30.9408), ("Gharbia", 30.7951, 31.0375), ("Monufia", 30.4669, 30.9319),
    ("Sharqia", 30.5877, 31.5020), ("Damietta", 31.4165, 31.8133), ("Port Said", 31.2565, 32.2841),
    ("Ismailia", 30.5965, 32.2715), ("Suez", 29.9737, 32.5263), ("North Sinai", 31.1325, 34.1917),
    ("South Sinai", 28.5031, 34.5129), ("Red Sea", 27.2579, 33.8116), ("Matrouh", 31.3525, 27.2453),
    ("New Valley", 25.6975, 28.8826), ("Beni Suef", 29.0744, 31.0975), ("Fayoum", 29.3084, 30.8441),
    ("Minya", 28.1099, 30.7503), ("Asyut", 27.1828, 31.1829), ("Sohag", 26.5569, 31.6948),
    ("Qena", 26.1551, 32.7165), ("Luxor", 25.6872, 32.6396), ("Aswan", 24.0908, 32.8994),
]

# --- API Models ---

class PaginationMetadata(BaseModel):
    """Pagination details returned with collection responses."""

    page: int = Field(description="Current page number.", examples=[1])
    page_size: int = Field(description="Maximum number of items on each page.", examples=[20])
    total_items: int = Field(description="Total number of cached items.", examples=[125])
    total_pages: int = Field(description="Total number of available pages.", examples=[7])


class Price(BaseModel):
    crop: str = Field(description="Crop name as reported by Bashaier.", examples=["Wheat"])
    min_price: float = Field(description="Minimum reported market price.", examples=[1200.5])
    max_price: float = Field(description="Maximum reported market price.", examples=[1500.0])
    average_price: float = Field(description="Average reported market price.", examples=[1350.25])


class Weather(BaseModel):
    governorate: str = Field(description="Egyptian governorate name.", examples=["Cairo"])
    temp: float = Field(description="Temperature in degrees Celsius.", examples=[34.5])
    feels_like: float = Field(description="Perceived temperature in degrees Celsius.", examples=[36.2])
    humidity: int = Field(description="Relative humidity percentage.", examples=[45])
    pressure: int = Field(description="Atmospheric pressure in hPa.", examples=[1012])
    description: str = Field(description="Human-readable weather condition.", examples=["clear sky"])
    main: str = Field(description="OpenWeather condition group.", examples=["Clear"])
    wind_speed: float = Field(description="Wind speed in metres per second.", examples=[3.1])
    visibility: int = Field(description="Visibility in metres.", examples=[10000])


class PricesResponse(BaseModel):
    prices: list[Price]
    pagination: PaginationMetadata
    last_updated: datetime | None = Field(
        description="UTC timestamp of the last successful cache update.",
        examples=["2026-08-13T14:30:00.123456+00:00"],
    )


class WeatherResponse(BaseModel):
    weather: list[Weather]
    pagination: PaginationMetadata
    last_updated: datetime | None = Field(
        description="UTC timestamp of the last successful cache update.",
        examples=["2026-08-13T14:30:05.654321+00:00"],
    )


class TriggerResponse(BaseModel):
    message: str


class ErrorResponse(BaseModel):
    detail: str = Field(
        description="A safe, human-readable explanation of the error.",
        examples=["Data cache is temporarily unavailable."],
    )


class RootResponse(BaseModel):
    message: str
    endpoints: dict[str, str]


Page = Annotated[
    int,
    Query(
        ge=1,
        description="Page number to return (starts at 1).",
        examples=[1],
    ),
]
PageSize = Annotated[
    int,
    Query(
        ge=1,
        le=100,
        description="Number of items per page (maximum 100).",
        examples=[20],
    ),
]

CACHE_ERROR_RESPONSES = {
    status.HTTP_500_INTERNAL_SERVER_ERROR: {
        "model": ErrorResponse,
        "description": "Cached data is malformed or could not be processed.",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "Redis is temporarily unavailable.",
        "headers": {
            "Retry-After": {
                "description": "Suggested delay in seconds before retrying.",
                "schema": {"type": "integer", "example": 30},
            },
        },
    },
}

# --- Helper Functions ---

class ScrapeIncompleteError(RuntimeError):
    """Raised when a refresh cannot produce a complete, trustworthy dataset."""


def _parse_number(value: str) -> float:
    """Parse Arabic/English digits and comma/dot decimals to float."""
    s = value.strip().replace(",", ".")
    if not s:
        return 0.0
    arabic = "٠١٢٣٤٥٦٧٨٩"
    for i, c in enumerate(arabic):
        s = s.replace(c, str(i))
    try:
        return float(s)
    except ValueError:
        return 0.0

def parse_price_page(html: str) -> tuple[list[dict], set[int]]:
    """Parse price rows and linked source-page numbers from one HTML document."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    table = soup.find("table")
    if table:
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue
            rows.append({
                "crop": cells[0].get_text(strip=True),
                "min_price": _parse_number(cells[1].get_text()),
                "max_price": _parse_number(cells[2].get_text()),
                "average_price": _parse_number(cells[3].get_text()),
            })

    source_path = urlparse(PRICE_LIST_BASE).path.rstrip("/")
    linked_pages: set[int] = set()
    for anchor in soup.select("a[href]"):
        parsed_url = urlparse(cast(str, anchor.get("href")))
        if parsed_url.path and parsed_url.path.rstrip("/") != source_path:
            continue
        for raw_page in parse_qs(parsed_url.query).get("page", []):
            try:
                page = int(raw_page)
            except ValueError:
                continue
            if page >= 1:
                linked_pages.add(page)

    return rows, linked_pages


def scrape_prices(html: str) -> list[dict]:
    """Parse the price table from bashaier.net HTML."""
    rows, _ = parse_price_page(html)
    return rows

def _client_headers() -> dict:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar,en;q=0.9",
    }


def _retry_delay(exc: httpx.HTTPError, attempt: int) -> float:
    """Calculate a bounded Retry-After or exponential-backoff delay."""
    if isinstance(exc, httpx.HTTPStatusError):
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(30.0, max(0.0, float(retry_after)))
            except ValueError:
                pass

    base_delay = HTTP_RETRY_BACKOFF_SECONDS * (2 ** attempt)
    return base_delay + random.uniform(0, base_delay * 0.25)


def _request_with_retries(
    client: httpx.Client,
    url: str,
    *,
    resource: str,
    params: dict | None = None,
) -> httpx.Response:
    """Make an upstream request, retrying only transient HTTP and network failures."""
    last_error: httpx.HTTPError | None = None

    for attempt in range(HTTP_MAX_ATTEMPTS):
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            last_error = exc
            status_code = exc.response.status_code
            retryable = status_code in {408, 429} or status_code >= 500
            if not retryable:
                raise ScrapeIncompleteError(
                    f"Upstream rejected {resource} with HTTP {status_code}."
                ) from exc
        except httpx.RequestError as exc:
            last_error = exc

        if attempt + 1 < HTTP_MAX_ATTEMPTS and last_error is not None:
            delay = _retry_delay(last_error, attempt)
            logger.warning(
                "Transient failure fetching %s; retrying in %.2f seconds (%d/%d)",
                resource,
                delay,
                attempt + 2,
                HTTP_MAX_ATTEMPTS,
            )
            time.sleep(delay)

    raise ScrapeIncompleteError(
        f"Could not fetch {resource} after {HTTP_MAX_ATTEMPTS} attempts."
    ) from last_error


def _http_limits() -> httpx.Limits:
    return httpx.Limits(
        max_connections=SCRAPE_MAX_WORKERS,
        max_keepalive_connections=SCRAPE_MAX_WORKERS,
    )


def _fetch_price_page(client: httpx.Client, page: int) -> tuple[list[dict], set[int]]:
    params = {"page": page, "value_chain_id": PRICE_VALUE_CHAIN_ID} if page > 1 else None
    response = _request_with_retries(
        client,
        PRICE_LIST_BASE,
        resource=f"price page {page}",
        params=params,
    )
    return parse_price_page(response.text)


def fetch_all_prices() -> list[dict]:
    """Crawl all linked price pages with bounded concurrency."""
    with httpx.Client(
        follow_redirects=True,
        timeout=HTTP_TIMEOUT_SECONDS,
        headers=_client_headers(),
        limits=_http_limits(),
    ) as client:
        first_rows, first_links = _fetch_price_page(client, 1)
        if not first_rows:
            raise ScrapeIncompleteError("Price page 1 contained no price rows.")

        rows_by_page = {1: first_rows}
        fingerprints = {json.dumps(first_rows, sort_keys=True)}
        pending_pages = first_links - {1}

        with ThreadPoolExecutor(max_workers=SCRAPE_MAX_WORKERS) as executor:
            while pending_pages:
                if max(pending_pages) > MAX_SOURCE_PAGES:
                    raise ScrapeIncompleteError(
                        f"Source pagination exceeds the {MAX_SOURCE_PAGES}-page safety limit."
                    )

                batch = sorted(pending_pages)
                pending_pages.clear()
                future_to_page = {
                    executor.submit(_fetch_price_page, client, page): page
                    for page in batch
                    if page not in rows_by_page
                }

                for future in as_completed(future_to_page):
                    page = future_to_page[future]
                    page_rows, linked_pages = future.result()
                    if not page_rows:
                        raise ScrapeIncompleteError(
                            f"Linked price page {page} contained no price rows."
                        )

                    fingerprint = json.dumps(page_rows, sort_keys=True)
                    if fingerprint in fingerprints:
                        raise ScrapeIncompleteError(
                            f"Price page {page} duplicated a previously fetched page."
                        )

                    rows_by_page[page] = page_rows
                    fingerprints.add(fingerprint)
                    pending_pages.update(linked_pages - rows_by_page.keys())

    return [row for page in sorted(rows_by_page) for row in rows_by_page[page]]

def fetch_weather_for_governorate(client: httpx.Client, name: str, lat: float, lon: float) -> dict | None:
    """Fetch current weather for a single governorate."""
    params = {
        "lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "en",
    }
    try:
        resp = _request_with_retries(
            client,
            OPENWEATHER_BASE,
            resource=f"weather for {name}",
            params=params,
        )
        data = resp.json()
        return {
            "governorate": name,
            "temp": round(data["main"]["temp"], 1),
            "feels_like": round(data["main"]["feels_like"], 1),
            "humidity": data["main"]["humidity"],
            "pressure": data["main"]["pressure"],
            "description": data["weather"][0]["description"],
            "main": data["weather"][0]["main"],
            "wind_speed": round(data.get("wind", {}).get("speed", 0), 1),
            "visibility": data.get("visibility", 0),
        }
    except (ScrapeIncompleteError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("Could not fetch valid weather for %s: %s", name, exc)
        return None

def fetch_all_weather() -> list[dict]:
    """Fetch weather for all Egyptian governorates."""
    if not OPENWEATHER_API_KEY:
        raise ValueError("OPENWEATHER_API_KEY is required in .env")

    results_by_index: dict[int, dict] = {}
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, limits=_http_limits()) as client:
        with ThreadPoolExecutor(max_workers=SCRAPE_MAX_WORKERS) as executor:
            future_to_index = {
                executor.submit(fetch_weather_for_governorate, client, name, lat, lon): index
                for index, (name, lat, lon) in enumerate(EGYPT_GOVERNORATES)
            }
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    result = future.result()
                except Exception:
                    logger.exception("Unexpected error fetching weather for index %d", index)
                    result = None
                if result is not None:
                    results_by_index[index] = result

    if len(results_by_index) != len(EGYPT_GOVERNORATES):
        missing_count = len(EGYPT_GOVERNORATES) - len(results_by_index)
        raise ScrapeIncompleteError(
            f"Weather refresh was incomplete ({missing_count} governorates missing)."
        )

    return [results_by_index[index] for index in range(len(EGYPT_GOVERNORATES))]


def paginate(items: list[dict], page: int, page_size: int) -> tuple[list[dict], PaginationMetadata]:
    """Return one page of items and its pagination metadata."""
    total_items = len(items)
    total_pages = math.ceil(total_items / page_size)
    start = (page - 1) * page_size
    return items[start:start + page_size], PaginationMetadata(
        page=page,
        page_size=page_size,
        total_items=total_items,
        total_pages=total_pages,
    )


def _cache_http_error(status_code: int, detail: str) -> HTTPException:
    headers = {"Retry-After": "30"} if status_code == status.HTTP_503_SERVICE_UNAVAILABLE else None
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


def read_cached_page(
    items_key: str,
    legacy_key: str,
    last_updated_key: str,
    page: int,
    page_size: int,
) -> tuple[list[dict], PaginationMetadata, str | None]:
    """Read only the requested Redis list range, with legacy JSON-cache fallback."""
    start = (page - 1) * page_size
    stop = start + page_size - 1

    try:
        pipeline = redis_client.pipeline(transaction=False)
        pipeline.exists(items_key)
        pipeline.llen(items_key)
        pipeline.lrange(items_key, start, stop)
        pipeline.get(legacy_key)
        pipeline.get(last_updated_key)
        item_key_exists, total_items, raw_items, legacy_data, last_updated = pipeline.execute()
    except redis.exceptions.ResponseError as exc:
        logger.exception("Redis data type is invalid for key %s", items_key)
        raise _cache_http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Cached data is invalid.",
        ) from exc
    except redis.exceptions.RedisError as exc:
        logger.exception("Redis read failed for key %s", items_key)
        raise _cache_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Data cache is temporarily unavailable.",
        ) from exc

    if item_key_exists:
        try:
            items = [json.loads(cast(str, item)) for item in cast(list, raw_items)]
        except (json.JSONDecodeError, TypeError) as exc:
            logger.exception("Cached list item is invalid for key %s", items_key)
            raise _cache_http_error(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Cached data is invalid.",
            ) from exc

        if not all(isinstance(item, dict) for item in items):
            exc = TypeError("Cached list contains a non-object item")
            logger.error("Cached list contains a non-object item for key %s", items_key)
            raise _cache_http_error(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Cached data is invalid.",
            ) from exc

        pagination = PaginationMetadata(
            page=page,
            page_size=page_size,
            total_items=cast(int, total_items),
            total_pages=math.ceil(cast(int, total_items) / page_size),
        )
        return items, pagination, cast(str | None, last_updated)

    if legacy_data is None:
        return [], PaginationMetadata(
            page=page,
            page_size=page_size,
            total_items=0,
            total_pages=0,
        ), None

    try:
        legacy_items = json.loads(cast(str, legacy_data))
    except (json.JSONDecodeError, TypeError) as exc:
        logger.exception("Legacy cached JSON is invalid for key %s", legacy_key)
        raise _cache_http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Cached data is invalid.",
        ) from exc

    if not isinstance(legacy_items, list):
        exc = TypeError("Legacy cached value is not a list")
        logger.error("Legacy cached value for key %s is not a list", legacy_key)
        raise _cache_http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Cached data is invalid.",
        ) from exc

    items, pagination = paginate(legacy_items, page, page_size)
    return items, pagination, cast(str | None, last_updated)


@contextmanager
def refresh_lock(lock_key: str) -> Iterator[bool]:
    """Acquire a non-blocking distributed lock for one refresh type."""
    lock = None
    try:
        lock = redis_client.lock(
            lock_key,
            timeout=SCRAPE_LOCK_TIMEOUT_SECONDS,
            blocking=False,
        )
        acquired = lock.acquire(blocking=False)
    except redis.exceptions.RedisError:
        logger.exception("Could not acquire refresh lock %s", lock_key)
        yield False
        return

    if not acquired:
        logger.info("Refresh skipped because lock %s is already held", lock_key)
        yield False
        return

    try:
        yield True
    finally:
        try:
            lock.release()
        except redis.exceptions.LockError:
            logger.warning("Refresh lock %s expired before release", lock_key)
        except redis.exceptions.RedisError:
            logger.exception("Could not release refresh lock %s", lock_key)


def store_collection(
    items_key: str,
    legacy_key: str,
    last_updated_key: str,
    items: list[dict],
) -> str:
    """Atomically replace a Redis list and its last-updated timestamp."""
    serialized_items = [json.dumps(item, separators=(",", ":")) for item in items]
    now = datetime.now(timezone.utc).isoformat()

    pipeline = redis_client.pipeline(transaction=True)
    pipeline.delete(items_key)
    pipeline.rpush(items_key, *serialized_items)
    pipeline.set(last_updated_key, now)
    pipeline.delete(legacy_key)
    pipeline.execute()
    return now

# --- Core Logic ---

def save_prices_to_redis() -> bool:
    """Scrape prices and save to Redis."""
    with refresh_lock(REDIS_PRICES_REFRESH_LOCK_KEY) as acquired:
        if not acquired:
            return False

        try:
            prices = fetch_all_prices()
            if not prices:
                raise ScrapeIncompleteError("Price refresh returned no data.")
            now = store_collection(
                REDIS_PRICES_ITEMS_KEY,
                REDIS_PRICES_KEY,
                REDIS_PRICES_LAST_UPDATED_KEY,
                prices,
            )
            logger.info("Saved %d prices to Redis at %s", len(prices), now)
            return True
        except ScrapeIncompleteError as exc:
            logger.warning("Price refresh rejected: %s", exc)
        except redis.exceptions.RedisError:
            logger.exception("Could not save prices to Redis")
        except Exception:
            logger.exception("Unexpected price refresh failure")
        return False


def save_weather_to_redis() -> bool:
    """Fetch weather and save to Redis."""
    with refresh_lock(REDIS_WEATHER_REFRESH_LOCK_KEY) as acquired:
        if not acquired:
            return False

        try:
            weather_list = fetch_all_weather()
            now = store_collection(
                REDIS_WEATHER_ITEMS_KEY,
                REDIS_WEATHER_KEY,
                REDIS_WEATHER_LAST_UPDATED_KEY,
                weather_list,
            )
            logger.info(
                "Saved weather for %d governorates to Redis at %s",
                len(weather_list),
                now,
            )
            return True
        except (ScrapeIncompleteError, ValueError) as exc:
            logger.warning("Weather refresh rejected: %s", exc)
        except redis.exceptions.RedisError:
            logger.exception("Could not save weather to Redis")
        except Exception:
            logger.exception("Unexpected weather refresh failure")
        return False


# --- FastAPI Application ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = BackgroundScheduler(timezone="UTC")
    job_defaults = {
        "trigger": "interval",
        "hours": 24,
        "next_run_time": datetime.now(timezone.utc),
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 3600,
    }
    scheduler.add_job(
        save_prices_to_redis,
        id="refresh-prices",
        name="Refresh agricultural prices",
        **job_defaults,
    )
    scheduler.add_job(
        save_weather_to_redis,
        id="refresh-weather",
        name="Refresh governorate weather",
        **job_defaults,
    )
    scheduler.start()
    logger.info("Background scheduler started; initial refreshes were queued")

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Background scheduler shut down")

tags_metadata = [
    {
        "name": "General",
        "description": "API discovery and service information.",
    },
    {
        "name": "Scraper data",
        "description": "Read paginated agricultural price and Egyptian weather data cached in Redis.",
    },
    {
        "name": "Scraper jobs",
        "description": "Manually start background jobs that refresh the Redis cache.",
    },
]

app = FastAPI(
    title="Bashaier & Egypt Weather API",
    summary="Agricultural prices and weather data for Egypt",
    description=(
        "A REST API that collects agricultural prices from Bashaier and current "
        "weather for Egyptian governorates, then serves the cached results from Redis."
    ),
    version="1.0.0",
    openapi_tags=tags_metadata,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200", "https://valor-labs.com/"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get(
    "/api/v1/",
    response_model=RootResponse,
    summary="Get API information",
    description="Returns the versioned paths exposed by this API.",
    tags=["General"],
)
def root():
    return {
        "message": "Welcome to Bashaier & Egypt Weather API",
        "endpoints": {
            "prices": "/api/v1/scraper/prices",
            "weather": "/api/v1/scraper/weather",
            "scrape_prices": "/api/v1/scraper/prices (POST)",
            "scrape_weather": "/api/v1/scraper/weather (POST)"
        }
    }

@app.get(
    "/api/v1/scraper/prices",
    response_model=PricesResponse,
    summary="List agricultural prices",
    description=(
        "Returns one page of the latest agricultural prices cached in Redis. "
        "Requesting a page after the final page returns an empty `prices` list."
    ),
    response_description="A page of agricultural prices and pagination metadata.",
    responses=CACHE_ERROR_RESPONSES,
    tags=["Scraper data"],
)
def get_prices(page: Page = 1, page_size: PageSize = 20) -> PricesResponse:
    prices, pagination, last_updated = read_cached_page(
        REDIS_PRICES_ITEMS_KEY,
        REDIS_PRICES_KEY,
        REDIS_PRICES_LAST_UPDATED_KEY,
        page,
        page_size,
    )

    try:
        return PricesResponse(
            prices=prices,
            pagination=pagination,
            last_updated=last_updated,
        )
    except ValidationError as exc:
        logger.exception("Cached price data failed response validation")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Cached data is invalid.",
        ) from exc

@app.get(
    "/api/v1/scraper/weather",
    response_model=WeatherResponse,
    summary="List governorate weather",
    description=(
        "Returns one page of the latest weather observations cached in Redis. "
        "Requesting a page after the final page returns an empty `weather` list."
    ),
    response_description="A page of weather observations and pagination metadata.",
    responses=CACHE_ERROR_RESPONSES,
    tags=["Scraper data"],
)
def get_weather(page: Page = 1, page_size: PageSize = 20) -> WeatherResponse:
    weather, pagination, last_updated = read_cached_page(
        REDIS_WEATHER_ITEMS_KEY,
        REDIS_WEATHER_KEY,
        REDIS_WEATHER_LAST_UPDATED_KEY,
        page,
        page_size,
    )

    try:
        return WeatherResponse(
            weather=weather,
            pagination=pagination,
            last_updated=last_updated,
        )
    except ValidationError as exc:
        logger.exception("Cached weather data failed response validation")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Cached data is invalid.",
        ) from exc

@app.post(
    "/api/v1/scraper/prices",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TriggerResponse,
    summary="Refresh agricultural prices",
    description="Queues a background job to scrape prices and update the Redis cache.",
    response_description="Confirmation that the refresh job was queued.",
    tags=["Scraper jobs"],
)
def trigger_price_scrape(background_tasks: BackgroundTasks):
    # BackgroundTasks allows the API to return a 202 Accepted immediately
    # while the heavy scraping task runs in a background thread.
    background_tasks.add_task(save_prices_to_redis)
    return {"message": "Price scrape triggered in the background."}

@app.post(
    "/api/v1/scraper/weather",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TriggerResponse,
    summary="Refresh governorate weather",
    description="Queues a background job to fetch current weather and update the Redis cache.",
    response_description="Confirmation that the refresh job was queued.",
    tags=["Scraper jobs"],
)
def trigger_weather_scrape(background_tasks: BackgroundTasks):
    background_tasks.add_task(save_weather_to_redis)
    return {"message": "Weather scrape triggered in the background."}
