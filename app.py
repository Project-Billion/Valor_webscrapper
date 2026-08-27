import os
import json
import math
import logging
import httpx
from bs4 import BeautifulSoup
import redis
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler #changing BlockingScheduler

# ===== KEKA =====
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, cast
# ===== KEKA =====


load_dotenv()

logger = logging.getLogger(__name__)

# --- Environment Variables ---
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") 
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")


# ===== KEKA =====
# --- Global Redis Client ---
redis_client: redis.Redis = redis.Redis(
    host=REDIS_HOST, 
    password=REDIS_PASSWORD, 
    port=REDIS_PORT, 
    decode_responses=True
)
# ===== KEKA =====



# --- Constants ---

# ---- Prices ----
REDIS_PRICES_KEY = "bashaier:prices"
REDIS_PRICES_LAST_UPDATED_KEY = "bashaier:prices:last_updated"
PRICE_LIST_BASE = "https://www.bashaier.net/market-price/price-list"
MAX_SOURCE_PAGES = 50

# ---- Weather ----
REDIS_WEATHER_KEY = "egypt:weather"
REDIS_WEATHER_LAST_UPDATED_KEY = "egypt:weather:last_updated"
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

def scrape_prices(html: str) -> list[dict]:
    """Parse the price table from bashaier.net HTML."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    table = soup.find("table")
    if not table:
        return rows
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
    return rows

def _client_headers() -> dict:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar,en;q=0.9",
    }

def fetch_all_prices() -> list[dict]:
    """Fetch all pages from source and return the full list."""
    all_prices = []
    with httpx.Client(
        follow_redirects=True,
        timeout=15.0,
        headers=_client_headers()
    ) as client:
        for p in range(1, MAX_SOURCE_PAGES + 1):
            url = f"{PRICE_LIST_BASE}?page={p}" if p > 1 else PRICE_LIST_BASE
            try:
                response = client.get(url)
                response.raise_for_status()
                rows = scrape_prices(response.text)
                if not rows:
                    break
                all_prices.extend(rows)
            except httpx.HTTPError:
                break
    return all_prices

def fetch_weather_for_governorate(client: httpx.Client, name: str, lat: float, lon: float) -> dict | None:
    """Fetch current weather for a single governorate."""
    params = {
        "lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "en",
    }
    try:
        resp = client.get(OPENWEATHER_BASE, params=params)
        resp.raise_for_status()
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
    except (httpx.HTTPError, KeyError, IndexError) as e:
        print(f"Error fetching {name}: {e}")
        return None

def fetch_all_weather() -> list[dict]:
    """Fetch weather for all Egyptian governorates."""
    if not OPENWEATHER_API_KEY:
        raise ValueError("OPENWEATHER_API_KEY is required in .env")

    results = []
    with httpx.Client(timeout=15.0) as client:
        for name, lat, lon in EGYPT_GOVERNORATES:
            w = fetch_weather_for_governorate(client, name, lat, lon)
            if w:
                results.append(w)
    return results


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


def read_cached_collection(data_key: str, last_updated_key: str) -> tuple[list[dict], str | None]:
    """Read and decode a cached collection, translating known failures to HTTP errors."""
    try:
        data = cast(str | None, redis_client.get(data_key))
        last_updated = cast(str | None, redis_client.get(last_updated_key))
    except redis.exceptions.RedisError as exc:
        logger.exception("Redis read failed for key %s", data_key)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Data cache is temporarily unavailable.",
            headers={"Retry-After": "30"},
        ) from exc

    if data is None:
        return [], None

    try:
        items = json.loads(data)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.exception("Cached JSON is invalid for key %s", data_key)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Cached data is invalid.",
        ) from exc

    if not isinstance(items, list):
        logger.error("Cached value for key %s is not a list", data_key)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Cached data is invalid.",
        )

    return items, last_updated

# --- Core Logic ---

def save_prices_to_redis() -> None:
    """Scrape prices and save to Redis."""
    try:
        prices = fetch_all_prices()
        if not prices:
            print("Warning: No prices scraped. Skipping Redis update.")
            return

        now = datetime.now(timezone.utc).isoformat()
        redis_client.set(REDIS_PRICES_KEY, json.dumps(prices))
        redis_client.set(REDIS_PRICES_LAST_UPDATED_KEY, now)
        print(f"Saved {len(prices)} prices to Redis at {now}")

    except redis.ConnectionError as e:
        print(f"Redis connection error: {e}")
    except Exception as e:
        print(f"Error scraping/saving prices: {e}")

def save_weather_to_redis() -> None:
    """Fetch weather and save to Redis."""
    try:
        if not OPENWEATHER_API_KEY:
            print("Skipping weather: OPENWEATHER_API_KEY not set in .env")
            return

        weather_list = fetch_all_weather()
        if not weather_list:
            print("Warning: No weather data fetched. Skipping Redis update.")
            return

        now = datetime.now(timezone.utc).isoformat()
        redis_client.set(REDIS_WEATHER_KEY, json.dumps(weather_list))
        redis_client.set(REDIS_WEATHER_LAST_UPDATED_KEY, now)
        print(f"Saved weather for {len(weather_list)} governorates to Redis at {now}")

    except redis.ConnectionError as e:
        print(f"Redis connection error: {e}")
    except Exception as e:
        print(f"Error fetching/saving weather: {e}")


# --- FastAPI Application ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Run initial fetch on startup
    print("Running initial data fetch...")
    save_prices_to_redis()
    save_weather_to_redis()

    # 2. Setup Background Scheduler
    print("Starting background scheduler...")
    scheduler = BackgroundScheduler()
    scheduler.add_job(save_prices_to_redis, "interval", hours=24)
    scheduler.add_job(save_weather_to_redis, "interval", hours=24)
    scheduler.start()
    
    yield # App is now running and serving requests
    
    # 3. Shutdown scheduler on exit
    scheduler.shutdown()
    print("Scheduler shut down.")

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
    prices, last_updated = read_cached_collection(
        REDIS_PRICES_KEY,
        REDIS_PRICES_LAST_UPDATED_KEY,
    )
    paginated_prices, pagination = paginate(prices, page, page_size)

    try:
        return PricesResponse(
            prices=paginated_prices,
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
    weather, last_updated = read_cached_collection(
        REDIS_WEATHER_KEY,
        REDIS_WEATHER_LAST_UPDATED_KEY,
    )
    paginated_weather, pagination = paginate(weather, page, page_size)

    try:
        return WeatherResponse(
            weather=paginated_weather,
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
