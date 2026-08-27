# Bashaier & Egypt Weather API Documentation

This document provides comprehensive documentation for the FastAPI application that scrapes agricultural prices from Bashaier and fetches weather data for Egyptian governorates, caching them in Redis.

##  Quick Start

* **Base URL:** `http://localhost:8000`
* **Interactive Swagger UI:** [http://localhost:8000/docs](http://localhost:8000/docs)
* **OpenAPI schema:** [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)
* **ReDoc:** [http://localhost:8000/redoc](http://localhost:8000/redoc)
* **Frontend integration guide:** [FRONTEND_API_DOCUMENTATION.md](FRONTEND_API_DOCUMENTATION.md)

---

##  Endpoints Overview

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/v1/` | API information and available endpoints. |
| `GET` | `/api/v1/scraper/prices` | Retrieve paginated agricultural prices from Redis. |
| `GET` | `/api/v1/scraper/weather` | Retrieve paginated weather data for Egyptian governorates. |
| `POST` | `/api/v1/scraper/prices` | Manually trigger a price scrape. Runs in the background. |
| `POST` | `/api/v1/scraper/weather` | Manually trigger a weather fetch. Runs in the background. |

---

##  Detailed Endpoints

### 1. API Root

**`GET /api/v1/`**

Returns basic information about the API and a list of available endpoints.

**Response Example:**

```json
{
  "message": "Welcome to Bashaier & Egypt Weather API",
  "endpoints": {
    "prices": "/api/v1/scraper/prices",
    "weather": "/api/v1/scraper/weather",
    "scrape_prices": "/api/v1/scraper/prices (POST)",
    "scrape_weather": "/api/v1/scraper/weather (POST)"
  }
}
```

---

### 2. Get Prices

**`GET /api/v1/scraper/prices`**

Retrieves one page of the latest cached agricultural prices from Redis.

The data is updated automatically every 24 hours or when manually triggered using `POST /api/v1/scraper/prices`.

**Query Parameters:**

| Name | Type | Default | Constraints | Description |
| :--- | :--- | :--- | :--- | :--- |
| `page` | integer | `1` | Minimum `1` | Page number to return. |
| `page_size` | integer | `20` | From `1` to `100` | Maximum items returned per page. |

Example request:

```http
GET /api/v1/scraper/prices?page=2&page_size=20
```

**Response Example:**

```json
{
  "prices": [
    {
      "crop": "Wheat",
      "min_price": 1200.50,
      "max_price": 1500.00,
      "average_price": 1350.25
    },
    {
      "crop": "Corn",
      "min_price": 800.00,
      "max_price": 950.00,
      "average_price": 875.00
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total_items": 125,
    "total_pages": 7
  },
  "last_updated": "2026-08-13T14:30:00.123456+00:00"
}
```

**Empty Cache Response Example:**

```json
{
  "prices": [],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total_items": 0,
    "total_pages": 0
  },
  "last_updated": null
}
```

**Error Responses:**

* `422 Unprocessable Content`: `page` or `page_size` is outside its allowed range.
* `500 Internal Server Error`: Cached data is malformed or fails response validation.
* `503 Service Unavailable`: Redis is unavailable. The response includes `Retry-After: 30`.

---

### 3. Get Weather

**`GET /api/v1/scraper/weather`**

Retrieves one page of the latest cached weather data for configured Egyptian governorates from Redis.

The data is updated automatically every 24 hours or when manually triggered using `POST /api/v1/scraper/weather`.

**Query Parameters:**

| Name | Type | Default | Constraints | Description |
| :--- | :--- | :--- | :--- | :--- |
| `page` | integer | `1` | Minimum `1` | Page number to return. |
| `page_size` | integer | `20` | From `1` to `100` | Maximum items returned per page. |

Example request:

```http
GET /api/v1/scraper/weather?page=1&page_size=10
```

**Response Example:**

```json
{
  "weather": [
    {
      "governorate": "Cairo",
      "temp": 34.5,
      "feels_like": 36.2,
      "humidity": 45,
      "pressure": 1012,
      "description": "clear sky",
      "main": "Clear",
      "wind_speed": 3.1,
      "visibility": 10000
    },
    {
      "governorate": "Alexandria",
      "temp": 29.8,
      "feels_like": 31.0,
      "humidity": 65,
      "pressure": 1014,
      "description": "few clouds",
      "main": "Clouds",
      "wind_speed": 4.5,
      "visibility": 10000
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total_items": 27,
    "total_pages": 2
  },
  "last_updated": "2026-08-13T14:30:05.654321+00:00"
}
```

**Empty Cache Response Example:**

```json
{
  "weather": [],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total_items": 0,
    "total_pages": 0
  },
  "last_updated": null
}
```

**Error Responses:**

* `422 Unprocessable Content`: `page` or `page_size` is outside its allowed range.
* `500 Internal Server Error`: Cached data is malformed or fails response validation.
* `503 Service Unavailable`: Redis is unavailable. The response includes `Retry-After: 30`.

---

### 4. Trigger Price Scrape

**`POST /api/v1/scraper/prices`**

Manually triggers the web scraper to fetch the latest prices from Bashaier.

Because scraping may take time, this endpoint immediately returns a response and runs the scraping process in a background task.

**Response Example:**

Status: `202 Accepted`

```json
{
  "message": "Price scrape triggered in the background."
}
```

---

### 5. Trigger Weather Fetch

**`POST /api/v1/scraper/weather`**

Manually triggers the weather fetching process for all Egyptian governorates.

Because fetching weather for multiple governorates may take time, this endpoint immediately returns a response and runs the task in the background.

**Response Example:**

Status: `202 Accepted`

```json
{
  "message": "Weather scrape triggered in the background."
}
```

---

##  Background Jobs & Scheduling

The application uses **APScheduler** to automate data collection.

### Startup Behavior

When the FastAPI application starts:

1. It starts serving API requests without waiting for upstream network calls.
2. It starts the background scheduler.
3. It queues initial price and weather refreshes on scheduler worker threads.
4. Each successful refresh atomically replaces its Redis list and timestamp.

Redis distributed locks prevent scheduled and manually triggered refreshes from running concurrently. Incomplete refreshes are rejected so they do not replace the last complete dataset.

### Scheduled Updates

The following jobs run automatically every **24 hours**:

* `save_prices_to_redis()`
* `save_weather_to_redis()`

### Manual Updates

You can manually refresh the data at any time using:

```http
POST /api/v1/scraper/prices
```

or

```http
POST /api/v1/scraper/weather
```

---

##  Redis Keys Used

| Redis Key | Description |
| :--- | :--- |
| `bashaier:prices:items:v2` | Redis list containing one JSON agricultural price per entry. |
| `bashaier:prices:last_updated` | Stores the UTC timestamp of the last successful price update. |
| `bashaier:prices:refresh:lock` | Temporary distributed lock for price refreshes. |
| `egypt:weather:items:v2` | Redis list containing one JSON weather observation per entry. |
| `egypt:weather:last_updated` | Stores the UTC timestamp of the last successful weather update. |
| `egypt:weather:refresh:lock` | Temporary distributed lock for weather refreshes. |

The API can read the legacy `bashaier:prices` and `egypt:weather` JSON keys until the first successful refresh migrates each dataset to its list key.

---

##  Environment Variables

The application expects the following environment variables:

```env
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_CONNECT_TIMEOUT_SECONDS=3
REDIS_SOCKET_TIMEOUT_SECONDS=5
OPENWEATHER_API_KEY=your_openweather_api_key
HTTP_TIMEOUT_SECONDS=15
HTTP_MAX_ATTEMPTS=3
HTTP_RETRY_BACKOFF_SECONDS=0.5
SCRAPE_MAX_WORKERS=5
SCRAPE_LOCK_TIMEOUT_SECONDS=1800
```

| Variable | Required | Description |
| :--- | :--- | :--- |
| `REDIS_HOST` | Optional | Redis host. Defaults to `localhost`. |
| `REDIS_PORT` | Optional | Redis port. Defaults to `6379`. |
| `REDIS_PASSWORD` | Optional | Redis password if authentication is enabled. |
| `REDIS_CONNECT_TIMEOUT_SECONDS` | Optional | Maximum Redis connection wait. Defaults to `3`. |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | Optional | Maximum Redis socket operation wait. Defaults to `5`. |
| `OPENWEATHER_API_KEY` | Required for weather | API key for OpenWeatherMap. |
| `HTTP_TIMEOUT_SECONDS` | Optional | Per-operation upstream HTTP timeout. Defaults to `15`. |
| `HTTP_MAX_ATTEMPTS` | Optional | Maximum attempts for transient upstream failures. Defaults to `3`. |
| `HTTP_RETRY_BACKOFF_SECONDS` | Optional | Initial exponential-backoff delay. Defaults to `0.5`. |
| `SCRAPE_MAX_WORKERS` | Optional | Maximum concurrent requests per scraper. Defaults to `5`. |
| `SCRAPE_LOCK_TIMEOUT_SECONDS` | Optional | Distributed refresh-lock lifetime. Defaults to `1800`. |

---

##  Running the Application

Install dependencies:

```bash
pip install fastapi uvicorn apscheduler httpx beautifulsoup4 redis python-dotenv
```

Run the API:

```bash
uvicorn app:app --reload
```

The API will be available at:

```text
http://localhost:8000
```

## Running Tests

```bash
python -m unittest discover -v
```
