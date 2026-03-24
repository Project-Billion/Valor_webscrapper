import os
import json
import httpx
from bs4 import BeautifulSoup
import redis
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler

load_dotenv()

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# --- Prices ---
REDIS_PRICES_KEY = "bashaier:prices"
REDIS_PRICES_LAST_UPDATED_KEY = "bashaier:prices:last_updated"
PRICE_LIST_BASE = "https://www.bashaier.net/market-price/price-list"
MAX_SOURCE_PAGES = 50

# --- Weather ---
REDIS_WEATHER_KEY = "egypt:weather"
REDIS_WEATHER_LAST_UPDATED_KEY = "egypt:weather:last_updated"
OPENWEATHER_BASE = "https://api.openweathermap.org/data/2.5/weather"
EGYPT_GOVERNORATES = [
    ("Cairo", 30.0444, 31.2357),
    ("Alexandria", 31.2001, 29.9187),
    ("Giza", 30.0131, 31.2089),
    ("Qalyubia", 30.1792, 31.2056),
    ("Dakahlia", 31.0409, 31.3785),
    ("Beheira", 31.0375, 30.4698),
    ("Kafr El Sheikh", 31.1117, 30.9408),
    ("Gharbia", 30.7951, 31.0375),
    ("Monufia", 30.4669, 30.9319),
    ("Sharqia", 30.5877, 31.5020),
    ("Damietta", 31.4165, 31.8133),
    ("Port Said", 31.2565, 32.2841),
    ("Ismailia", 30.5965, 32.2715),
    ("Suez", 29.9737, 32.5263),
    ("North Sinai", 31.1325, 34.1917),
    ("South Sinai", 28.5031, 34.5129),
    ("Red Sea", 27.2579, 33.8116),
    ("Matrouh", 31.3525, 27.2453),
    ("New Valley", 25.6975, 28.8826),
    ("Beni Suef", 29.0744, 31.0975),
    ("Fayoum", 29.3084, 30.8441),
    ("Minya", 28.1099, 30.7503),
    ("Asyut", 27.1828, 31.1829),
    ("Sohag", 26.5569, 31.6948),
    ("Qena", 26.1551, 32.7165),
    ("Luxor", 25.6872, 32.6396),
    ("Aswan", 24.0908, 32.8994),
]


# --- Prices ---
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
        headers=_client_headers(),
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


def save_prices_to_redis() -> None:
    """Scrape prices and save to Redis."""
    from datetime import datetime, timezone

    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        prices = fetch_all_prices()
        if not prices:
            print("Warning: No prices scraped. Skipping Redis update.")
            return

        now = datetime.now(timezone.utc).isoformat()
        r.set(REDIS_PRICES_KEY, json.dumps(prices))
        r.set(REDIS_PRICES_LAST_UPDATED_KEY, now)
        print(f"Saved {len(prices)} prices to Redis at {now}")

    except redis.ConnectionError as e:
        print(f"Redis connection error: {e}")
    except Exception as e:
        print(f"Error scraping/saving prices: {e}")


# --- Weather ---
def fetch_weather_for_governorate(
    client: httpx.Client, name: str, lat: float, lon: float
) -> dict | None:
    """Fetch current weather for a single governorate."""
    params = {
        "lat": lat,
        "lon": lon,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "lang": "en",
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


def save_weather_to_redis() -> None:
    """Fetch weather and save to Redis."""
    from datetime import datetime, timezone

    try:
        if not OPENWEATHER_API_KEY:
            print("Skipping weather: OPENWEATHER_API_KEY not set in .env")
            return

        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        weather_list = fetch_all_weather()
        if not weather_list:
            print("Warning: No weather data fetched. Skipping Redis update.")
            return

        now = datetime.now(timezone.utc).isoformat()
        r.set(REDIS_WEATHER_KEY, json.dumps(weather_list))
        r.set(REDIS_WEATHER_LAST_UPDATED_KEY, now)
        print(f"Saved weather for {len(weather_list)} governorates to Redis at {now}")

    except redis.ConnectionError as e:
        print(f"Redis connection error: {e}")
    except Exception as e:
        print(f"Error fetching/saving weather: {e}")


def main():
    print("Starting daily scraper (prices + weather)...")
    save_prices_to_redis()
    save_weather_to_redis()

    scheduler = BlockingScheduler()
    scheduler.add_job(save_prices_to_redis, "interval", hours=24)
    scheduler.add_job(save_weather_to_redis, "interval", hours=24)
    scheduler.start()


if __name__ == "__main__":
    main()
