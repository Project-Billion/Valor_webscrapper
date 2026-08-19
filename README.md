# Bashaier & Egypt Weather API Documentation

This document provides comprehensive documentation for the FastAPI application that scrapes agricultural prices from Bashaier and fetches weather data for Egyptian governorates, caching them in Redis.

##  Quick Start

* **Base URL:** `http://localhost:8000`
* **Interactive Swagger UI:** [http://localhost:8000/docs](http://localhost:8000/docs)
* **ReDoc:** [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

##  Endpoints Overview

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/` | API information and available endpoints. |
| `GET` | `/prices` | Retrieve cached agricultural prices from Redis. |
| `GET` | `/weather` | Retrieve cached weather data for Egyptian governorates. |
| `POST` | `/scrape/prices` | Manually trigger a price scrape. Runs in the background. |
| `POST` | `/scrape/weather` | Manually trigger a weather fetch. Runs in the background. |

---

##  Detailed Endpoints

### 1. API Root

**`GET /`**

Returns basic information about the API and a list of available endpoints.

**Response Example:**

```json
{
  "message": "Welcome to Bashaier & Egypt Weather API",
  "endpoints": {
    "prices": "/prices",
    "weather": "/weather",
    "scrape_prices": "/scrape/prices (POST)",
    "scrape_weather": "/scrape/weather (POST)"
  }
}
```

---

### 2. Get Prices

**`GET /prices`**

Retrieves the latest cached agricultural prices from Redis.

The data is updated automatically every 24 hours or when manually triggered using `POST /scrape/prices`.

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
  "last_updated": "2026-08-13T14:30:00.123456+00:00"
}
```

**Empty Cache Response Example:**

```json
{
  "prices": [],
  "last_updated": null
}
```

**Error Responses:**

* `500 Internal Server Error`: Failed to connect to Redis.

---

### 3. Get Weather

**`GET /weather`**

Retrieves the latest cached weather data for all configured Egyptian governorates from Redis.

The data is updated automatically every 24 hours or when manually triggered using `POST /scrape/weather`.

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
  "last_updated": "2026-08-13T14:30:05.654321+00:00"
}
```

**Empty Cache Response Example:**

```json
{
  "weather": [],
  "last_updated": null
}
```

**Error Responses:**

* `500 Internal Server Error`: Failed to connect to Redis.

---

### 4. Trigger Price Scrape

**`POST /scrape/prices`**

Manually triggers the web scraper to fetch the latest prices from Bashaier.

Because scraping may take time, this endpoint immediately returns a response and runs the scraping process in a background task.

**Response Example:**

```json
{
  "message": "Price scrape triggered in the background."
}
```

---

### 5. Trigger Weather Fetch

**`POST /scrape/weather`**

Manually triggers the weather fetching process for all Egyptian governorates.

Because fetching weather for multiple governorates may take time, this endpoint immediately returns a response and runs the task in the background.

**Response Example:**

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

1. It immediately fetches prices from Bashaier.
2. It immediately fetches weather data from OpenWeatherMap.
3. It stores both datasets in Redis.
4. It starts a background scheduler.

### Scheduled Updates

The following jobs run automatically every **24 hours**:

* `save_prices_to_redis()`
* `save_weather_to_redis()`

### Manual Updates

You can manually refresh the data at any time using:

```http
POST /scrape/prices
```

or

```http
POST /scrape/weather
```

---

##  Redis Keys Used

| Redis Key | Description |
| :--- | :--- |
| `bashaier:prices` | Stores the latest scraped agricultural prices as JSON. |
| `bashaier:prices:last_updated` | Stores the UTC timestamp of the last successful price update. |
| `egypt:weather` | Stores the latest weather data for Egyptian governorates as JSON. |
| `egypt:weather:last_updated` | Stores the UTC timestamp of the last successful weather update. |

---

##  Environment Variables

The application expects the following environment variables:

```env
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
OPENWEATHER_API_KEY=your_openweather_api_key
```

| Variable | Required | Description |
| :--- | :--- | :--- |
| `REDIS_HOST` | Optional | Redis host. Defaults to `localhost`. |
| `REDIS_PORT` | Optional | Redis port. Defaults to `6379`. |
| `REDIS_PASSWORD` | Optional | Redis password if authentication is enabled. |
| `OPENWEATHER_API_KEY` | Required for weather | API key for OpenWeatherMap. |

---

##  Running the Application

Install dependencies:

```bash
pip install fastapi uvicorn apscheduler httpx beautifulsoup4 redis python-dotenv
```

Run the API:

```bash
uvicorn main:app --reload
```

The API will be available at:

```text
http://localhost:8000
```