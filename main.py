import asyncio
import os
import time
from datetime import date as Date
from typing import Any, Literal, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field
from dotenv import load_dotenv

load_dotenv()

SALESFINDER_BASE_URL = os.getenv("SALESFINDER_BASE_URL", "https://api.salesfinder.ru")
SALESFINDER_API_VERSION = os.getenv("SALESFINDER_API_VERSION", "2025_09")
SALESFINDER_EMAIL = os.getenv("SALESFINDER_EMAIL", "")
SALESFINDER_PASSWORD = os.getenv("SALESFINDER_PASSWORD", "")
CONNECTOR_API_KEY = os.getenv("CONNECTOR_API_KEY", "")
SF_MIN_INTERVAL_SECONDS = float(os.getenv("SF_MIN_INTERVAL_SECONDS", "5.1"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "40"))

app = FastAPI(
    title="SalesFinder Read-only Connector for ChatGPT",
    version="1.0.0",
    description=(
        "Read-only proxy between a Custom GPT Action and SalesFinder API. "
        "It keeps SalesFinder credentials server-side, caches the Bearer token "
        "and enforces a minimum interval between SalesFinder requests."
    ),
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: Optional[str] = Security(api_key_header)) -> None:
    if not CONNECTOR_API_KEY:
        raise HTTPException(status_code=500, detail="CONNECTOR_API_KEY is not configured")
    if not api_key or api_key != CONNECTOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


class RangeNumber(BaseModel):
    min: Optional[float] = None
    max: Optional[float] = None


class ProductRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    mp: Literal["wb", "ozon"] = Field(description="Marketplace")
    sku: list[int] = Field(min_length=1, max_length=50, description="Marketplace SKU/article IDs")
    date: Date = Field(description="Start date")
    date2: Date = Field(description="End date")
    fbs: Literal[0, 1] = Field(default=0, description="Include seller warehouse/FBS data")


class CategorySearchRequest(BaseModel):
    marketPlace: Literal["wb", "ozon"]
    dateFrom: Date
    dateTo: Date
    categoryName: Optional[str] = None
    take: int = Field(default=10, ge=1, le=100)
    skip: int = Field(default=0, ge=0)


class CategoryReportRequest(BaseModel):
    marketPlace: Literal["wb", "ozon"]
    category: int = Field(description="SalesFinder category ID")
    dateFrom: Date
    dateTo: Date
    fbs: bool = False


class CategoryFilter(BaseModel):
    title: Optional[str] = None
    sku: Optional[str] = None
    position: Optional[RangeNumber] = None
    categories: Optional[RangeNumber] = None
    p_reviews: Optional[RangeNumber] = Field(default=None, description="Total reviews")
    rating: Optional[RangeNumber] = None
    remains: Optional[RangeNumber] = None
    sold: Optional[RangeNumber] = None
    revenue: Optional[RangeNumber] = None
    avg_sold: Optional[RangeNumber] = None
    avg_revenue: Optional[RangeNumber] = None
    days: Optional[RangeNumber] = None
    losses: Optional[RangeNumber] = None
    price: Optional[RangeNumber] = None
    keywords: Optional[RangeNumber] = None
    new_reviews: Optional[RangeNumber] = None
    brand: Optional[list[str]] = None
    seller: Optional[list[str]] = None


class CategoryProductsRequest(BaseModel):
    reportId: str
    take: int = Field(default=20, ge=1, le=100)
    skip: int = Field(default=0, ge=0)
    sort: Optional[str] = Field(default="revenue", description="SalesFinder sort field, e.g. revenue, sold, price")
    sort_dir: Literal["asc", "desc"] = "desc"
    filter: Optional[CategoryFilter] = None


class CategoryOverviewRequest(BaseModel):
    reportId: str
    filter: Optional[CategoryFilter] = None


class SalesFinderClient:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._token_valid_until = 0.0
        self._rate_lock = asyncio.Lock()
        self._auth_lock = asyncio.Lock()
        self._last_sf_request = 0.0

    @property
    def api_root(self) -> str:
        return f"{SALESFINDER_BASE_URL.rstrip('/')}/api/{SALESFINDER_API_VERSION}"

    async def _respect_rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            wait_for = SF_MIN_INTERVAL_SECONDS - (now - self._last_sf_request)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_sf_request = time.monotonic()

    @staticmethod
    def _extract_token(payload: Any) -> Optional[str]:
        """Robustly find a token without depending on one undocumented response shape."""
        preferred_keys = {
            "access_token", "accessToken", "token", "bearer", "jwt", "auth_token", "authToken"
        }

        def walk(value: Any) -> Optional[str]:
            if isinstance(value, dict):
                for key in preferred_keys:
                    candidate = value.get(key)
                    if isinstance(candidate, str) and len(candidate) > 20:
                        return candidate.removeprefix("Bearer ").strip()
                for child in value.values():
                    result = walk(child)
                    if result:
                        return result
            elif isinstance(value, list):
                for child in value:
                    result = walk(child)
                    if result:
                        return result
            return None

        return walk(payload)

    async def _login(self) -> str:
        if not SALESFINDER_EMAIL or not SALESFINDER_PASSWORD:
            raise HTTPException(
                status_code=500,
                detail="SALESFINDER_EMAIL / SALESFINDER_PASSWORD are not configured",
            )

        async with self._auth_lock:
            if self._token and time.monotonic() < self._token_valid_until:
                return self._token

            await self._respect_rate_limit()
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{self.api_root}/auth/login",
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    json={"email": SALESFINDER_EMAIL, "password": SALESFINDER_PASSWORD},
                )

            if response.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"SalesFinder login failed with HTTP {response.status_code}",
                )

            try:
                payload = response.json()
            except ValueError:
                raise HTTPException(status_code=502, detail="SalesFinder login returned non-JSON response")

            token = self._extract_token(payload)
            if not token:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "SalesFinder login succeeded but token was not found in the response. "
                        "Check the current SalesFinder login response format."
                    ),
                )

            self._token = token
            # SalesFinder docs say 3 hours. Re-login a little earlier for safety.
            self._token_valid_until = time.monotonic() + (2 * 60 * 60 + 50 * 60)
            return token

    async def post(self, path: str, body: dict[str, Any]) -> Any:
        token = await self._login()

        for attempt in (1, 2):
            await self._respect_rate_limit()
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{self.api_root}/{path.lstrip('/')}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )

            if response.status_code in (401, 403) and attempt == 1:
                self._token = None
                self._token_valid_until = 0
                token = await self._login()
                continue

            try:
                payload = response.json()
            except ValueError:
                raise HTTPException(
                    status_code=502,
                    detail=f"SalesFinder returned non-JSON response (HTTP {response.status_code})",
                )

            if response.status_code >= 400:
                # Return upstream business error, but never credentials/tokens.
                raise HTTPException(
                    status_code=502,
                    detail={
                        "salesfinder_http_status": response.status_code,
                        "salesfinder_response": payload,
                    },
                )

            return payload

        raise HTTPException(status_code=502, detail="SalesFinder authentication retry failed")


sf = SalesFinderClient()


def body_json(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


@app.get("/health", operation_id="healthCheck", tags=["system"])
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "salesfinder_api_version": SALESFINDER_API_VERSION,
        "credentials_configured": bool(SALESFINDER_EMAIL and SALESFINDER_PASSWORD),
        "connector_key_configured": bool(CONNECTOR_API_KEY),
    }


@app.post(
    "/sf/product/info",
    operation_id="getProductInfo",
    tags=["products"],
    dependencies=[Depends(require_api_key)],
)
async def product_info(body: ProductRequest) -> Any:
    """Static product data: title, brand, seller, marketplace URL, first/last seen dates."""
    return await sf.post("product/info", body_json(body))


@app.post(
    "/sf/product/overview",
    operation_id="getProductOverview",
    tags=["products"],
    dependencies=[Depends(require_api_key)],
)
async def product_overview(body: ProductRequest) -> Any:
    """Aggregated metrics for SKU list: price, sales, revenue, reviews, rating, stock, keywords."""
    return await sf.post("product/overview", body_json(body))


@app.post(
    "/sf/product/days",
    operation_id="getProductDailyMetrics",
    tags=["products"],
    dependencies=[Depends(require_api_key)],
)
async def product_days(body: ProductRequest) -> Any:
    """Daily dynamics: sales, revenue, stock, price, reviews and rating."""
    return await sf.post("product/days", body_json(body))


@app.post(
    "/sf/product/keywords",
    operation_id="getProductKeywords",
    tags=["products"],
    dependencies=[Depends(require_api_key)],
)
async def product_keywords(body: ProductRequest) -> Any:
    """Search queries/keywords and SKU positions for the selected period."""
    return await sf.post("product/keywords", body_json(body))


@app.post(
    "/sf/category/search",
    operation_id="searchCategories",
    tags=["categories"],
    dependencies=[Depends(require_api_key)],
)
async def category_search(body: CategorySearchRequest) -> Any:
    """Find SalesFinder category IDs by marketplace, period and optional category name."""
    return await sf.post("ext-analitic/show_category", body_json(body))


@app.post(
    "/sf/category/report",
    operation_id="createCategoryReport",
    tags=["categories"],
    dependencies=[Depends(require_api_key)],
)
async def category_report(body: CategoryReportRequest) -> Any:
    """Create a SalesFinder category report. Response contains reportId; report may need time to build."""
    return await sf.post("ext-analitic/category_report", body_json(body))


@app.post(
    "/sf/category/overview",
    operation_id="getCategoryOverview",
    tags=["categories"],
    dependencies=[Depends(require_api_key)],
)
async def category_overview(body: CategoryOverviewRequest) -> Any:
    """Get category-level totals/dynamics for an already-created reportId."""
    return await sf.post("ext-analitic/category_overview_all", body_json(body))


@app.post(
    "/sf/category/products",
    operation_id="getCategoryProducts",
    tags=["categories"],
    dependencies=[Depends(require_api_key)],
)
async def category_products(body: CategoryProductsRequest) -> Any:
    """Get products from a category report, with common analytical filters and sorting."""
    return await sf.post("ext-analitic/get_category_product", body_json(body))
