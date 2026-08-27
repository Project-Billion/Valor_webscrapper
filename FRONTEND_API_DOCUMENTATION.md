# Bashaier & Egypt Weather API

Frontend integration contract for API version `v1`.

## Access

| Resource | Local URL |
| :--- | :--- |
| API base URL | `http://localhost:8000/api/v1` |
| Swagger UI | `http://localhost:8000/docs` |
| OpenAPI JSON | `http://localhost:8000/openapi.json` |
| ReDoc | `http://localhost:8000/redoc` |

Replace `http://localhost:8000` with the deployed API origin in non-local environments.

All request and response bodies use `application/json`. Timestamps are ISO 8601 UTC strings.

## TypeScript Interfaces

```ts
export interface PaginationMetadata {
  page: number;
  page_size: number;
  total_items: number;
  total_pages: number;
}

export interface Price {
  crop: string;
  min_price: number;
  max_price: number;
  average_price: number;
}

export interface PricesResponse {
  prices: Price[];
  pagination: PaginationMetadata;
  last_updated: string | null;
}

export interface WeatherObservation {
  governorate: string;
  temp: number;
  feels_like: number;
  humidity: number;
  pressure: number;
  description: string;
  main: string;
  wind_speed: number;
  visibility: number;
}

export interface WeatherResponse {
  weather: WeatherObservation[];
  pagination: PaginationMetadata;
  last_updated: string | null;
}

export interface TriggerResponse {
  message: string;
}

export interface ApiError {
  detail: string;
}

export interface ValidationErrorItem {
  loc: Array<string | number>;
  msg: string;
  type: string;
  input?: unknown;
  ctx?: Record<string, unknown>;
}

export interface ValidationErrorResponse {
  detail: ValidationErrorItem[];
}
```

## Pagination

Both collection endpoints accept the following query parameters:

| Parameter | Type | Default | Valid range | Description |
| :--- | :--- | :--- | :--- | :--- |
| `page` | integer | `1` | `1` or greater | One-based page number. |
| `page_size` | integer | `20` | `1` through `100` | Maximum items returned in a page. |

Requesting a page beyond `total_pages` is valid. The API returns `200 OK`, an empty collection, and pagination metadata containing the requested page and the actual totals.

When the cache contains no items, `total_items` and `total_pages` are both `0`.

## Endpoints

### Get API information

```http
GET /api/v1/
```

Successful response: `200 OK`

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

### Get agricultural prices

```http
GET /api/v1/scraper/prices?page=1&page_size=20
```

Successful response: `200 OK`

```json
{
  "prices": [
    {
      "crop": "Wheat",
      "min_price": 1200.5,
      "max_price": 1500.0,
      "average_price": 1350.25
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total_items": 125,
    "total_pages": 7
  },
  "last_updated": "2026-08-27T10:00:00+00:00"
}
```

### Get governorate weather

```http
GET /api/v1/scraper/weather?page=1&page_size=20
```

Successful response: `200 OK`

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
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total_items": 27,
    "total_pages": 2
  },
  "last_updated": "2026-08-27T10:05:00+00:00"
}
```

Temperatures are in degrees Celsius, pressure is in hPa, wind speed is in metres per second, and visibility is in metres.

### Refresh agricultural prices

```http
POST /api/v1/scraper/prices
```

Successful response: `202 Accepted`

```json
{
  "message": "Price scrape triggered in the background."
}
```

The response confirms that the job was queued. It does not mean the cache refresh has completed. Poll the GET endpoint and compare `last_updated` to detect completion.

### Refresh governorate weather

```http
POST /api/v1/scraper/weather
```

Successful response: `202 Accepted`

```json
{
  "message": "Weather scrape triggered in the background."
}
```

The response confirms that the job was queued. Poll the GET endpoint and compare `last_updated` to detect completion.

## Status Codes and Errors

| Status | Applies to | Meaning | Frontend handling |
| :--- | :--- | :--- | :--- |
| `200 OK` | GET | The request succeeded. An empty collection is still successful. | Render the returned page. |
| `202 Accepted` | POST | The refresh job was queued for background execution. | Show a queued/refreshing state and poll if completion matters. |
| `422 Unprocessable Content` | Paginated GET | A query parameter has the wrong type or is outside its valid range. | Correct the request; do not retry unchanged. |
| `500 Internal Server Error` | Paginated GET | Cached data is malformed or cannot be validated. | Show a generic error; do not automatically retry repeatedly. |
| `503 Service Unavailable` | Paginated GET | Redis is temporarily unavailable. | Read `Retry-After` and retry after that delay. |

Sanitized `500` response:

```json
{
  "detail": "Cached data is invalid."
}
```

`503` response:

```http
HTTP/1.1 503 Service Unavailable
Retry-After: 30
Content-Type: application/json
```

```json
{
  "detail": "Data cache is temporarily unavailable."
}
```

Example `422` response:

```json
{
  "detail": [
    {
      "type": "greater_than_equal",
      "loc": ["query", "page"],
      "msg": "Input should be greater than or equal to 1",
      "input": "0",
      "ctx": {
        "ge": 1
      }
    }
  ]
}
```

## Fetch Examples

```ts
const API_BASE_URL = "http://localhost:8000/api/v1";

export async function getPrices(
  page = 1,
  pageSize = 20,
): Promise<PricesResponse> {
  const query = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });

  const response = await fetch(
    `${API_BASE_URL}/scraper/prices?${query.toString()}`,
  );

  if (!response.ok) {
    const error = (await response.json()) as ApiError | ValidationErrorResponse;
    throw new Error(JSON.stringify(error.detail));
  }

  return response.json() as Promise<PricesResponse>;
}
```

For a `503` response, use the `Retry-After` header value as seconds:

```ts
const retryAfterSeconds = Number(response.headers.get("Retry-After") ?? "30");
```

## Cache and Refresh Behavior

- Prices and weather are read from Redis rather than fetched from upstream services during GET requests.
- Both datasets are refreshed automatically every 24 hours.
- A refresh also runs when the API starts.
- POST endpoints queue an additional refresh in the background.
- `last_updated` is `null` until a successful dataset has been cached.
- A successful cache update changes `last_updated`.
- If a refresh produces no data, the existing cached dataset is retained.
