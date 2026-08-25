import asyncio
import os
import time
from typing import Any, Literal, Optional

import httpx
from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

load_dotenv()

SALESFINDER_BASE_URL = os.getenv("SALESFINDER_BASE_URL", "https://api.salesfinder.ru")
SALESFINDER_API_VERSION = os.getenv("SALESFINDER_API_VERSION", "2025_09")
SALESFINDER_EMAIL = os.getenv("SALESFINDER_EMAIL", "")
SALESFINDER_PASSWORD = os.getenv("SALESFINDER_PASSWORD", "")
# Reuse the key you already created for the old ChatGPT connector.
MCP_URL_SECRET = os.getenv("MCP_URL_SECRET") or os.getenv("CONNECTOR_API_KEY", "")
SF_MIN_INTERVAL_SECONDS = float(os.getenv("SF_MIN_INTERVAL_SECONDS", "5.1"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "45"))

if not MCP_URL_SECRET:
    raise RuntimeError("Set CONNECTOR_API_KEY (existing value is fine) or MCP_URL_SECRET in Render")

if "/" in MCP_URL_SECRET or "?" in MCP_URL_SECRET or "#" in MCP_URL_SECRET:
    raise RuntimeError("CONNECTOR_API_KEY/MCP_URL_SECRET must not contain /, ?, or #")


class SalesFinderError(RuntimeError):
    pass


class SalesFinderClient:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._token_valid_until = 0.0
        self._rate_lock = asyncio.Lock()
        self._auth_lock = asyncio.Lock()
        self._last_request = 0.0

    @property
    def api_root(self) -> str:
        return f"{SALESFINDER_BASE_URL.rstrip('/')}/api/{SALESFINDER_API_VERSION}"

    async def _rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            delay = SF_MIN_INTERVAL_SECONDS - (now - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()

    @staticmethod
    def _find_token(payload: Any) -> Optional[str]:
        preferred = {
            "access_token", "accessToken", "token", "bearer", "jwt",
            "auth_token", "authToken"
        }

        def walk(value: Any) -> Optional[str]:
            if isinstance(value, dict):
                for key in preferred:
                    candidate = value.get(key)
                    if isinstance(candidate, str) and len(candidate) > 20:
                        return candidate.removeprefix("Bearer ").strip()
                for child in value.values():
                    found = walk(child)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = walk(child)
                    if found:
                        return found
            return None

        return walk(payload)

    async def _login(self) -> str:
        if not SALESFINDER_EMAIL or not SALESFINDER_PASSWORD:
            raise SalesFinderError("SalesFinder login/password are not configured in Render")

        async with self._auth_lock:
            if self._token and time.monotonic() < self._token_valid_until:
                return self._token

            await self._rate_limit()
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{self.api_root}/auth/login",
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    json={"email": SALESFINDER_EMAIL, "password": SALESFINDER_PASSWORD},
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise SalesFinderError(
                    f"SalesFinder login returned non-JSON response (HTTP {response.status_code})"
                ) from exc

            if response.status_code >= 400:
                message = payload.get("message") if isinstance(payload, dict) else None
                raise SalesFinderError(
                    f"SalesFinder login failed (HTTP {response.status_code}): {message or 'unknown error'}"
                )

            token = self._find_token(payload)
            if not token:
                raise SalesFinderError(
                    "SalesFinder login succeeded, but token was not found in its response"
                )

            self._token = token
            # Docs: token is valid for 3 hours. Re-login slightly before expiry.
            self._token_valid_until = time.monotonic() + (2 * 60 * 60 + 50 * 60)
            return token

    async def post(self, path: str, body: dict[str, Any]) -> Any:
        token = await self._login()

        for attempt in (1, 2):
            await self._rate_limit()
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
                self._token_valid_until = 0.0
                token = await self._login()
                continue

            try:
                payload = response.json()
            except ValueError as exc:
                raise SalesFinderError(
                    f"SalesFinder returned non-JSON response (HTTP {response.status_code})"
                ) from exc

            if response.status_code >= 400:
                message = payload.get("message") if isinstance(payload, dict) else None
                raise SalesFinderError(
                    f"SalesFinder request failed (HTTP {response.status_code}): {message or payload}"
                )

            return payload

        raise SalesFinderError("SalesFinder authentication retry failed")


sf = SalesFinderClient()

mcp = MCPServer(
    "SalesFinder Analytics",
    instructions=(
        "Read-only SalesFinder analytics for Wildberries and Ozon. "
        "Use product_overview for aggregate SKU metrics, product_daily_metrics for dynamics, "
        "product_keywords for search terms, and category tools for niche/category analysis. "
        "Never invent metrics missing from SalesFinder. Category reports are asynchronous: "
        "first search_categories, then create_category_report, then use the returned reportId."
    ),
)


@mcp.tool()
async def product_overview(
    marketplace: Literal["wb", "ozon"],
    sku: list[int],
    date_from: str,
    date_to: str,
    include_fbs: bool = False,
) -> Any:
    """Get aggregate SKU metrics: price, sales, revenue, stock, reviews, rating and other overview data.

    Dates must be YYYY-MM-DD. sku is a list of marketplace article/SKU IDs.
    """
    return await sf.post(
        "product/overview",
        {
            "mp": marketplace,
            "date": date_from,
            "date2": date_to,
            "sku": sku,
            "fbs": 1 if include_fbs else 0,
        },
    )


@mcp.tool()
async def product_daily_metrics(
    marketplace: Literal["wb", "ozon"],
    sku: list[int],
    date_from: str,
    date_to: str,
    include_fbs: bool = False,
) -> Any:
    """Get daily SKU dynamics: sales, revenue, price, stock, reviews and rating."""
    return await sf.post(
        "product/days",
        {
            "mp": marketplace,
            "date": date_from,
            "date2": date_to,
            "sku": sku,
            "fbs": 1 if include_fbs else 0,
        },
    )


@mcp.tool()
async def product_keywords(
    marketplace: Literal["wb", "ozon"],
    sku: list[int],
    date_from: str,
    date_to: str,
    include_fbs: bool = False,
) -> Any:
    """Get product search keywords/queries and available position data for the period."""
    return await sf.post(
        "product/keywords",
        {
            "mp": marketplace,
            "date": date_from,
            "date2": date_to,
            "sku": sku,
            "fbs": 1 if include_fbs else 0,
        },
    )


@mcp.tool()
async def search_categories(
    marketplace: Literal["wb", "ozon"],
    date_from: str,
    date_to: str,
    category_name: Optional[str] = None,
    take: int = 10,
    skip: int = 0,
) -> Any:
    """Find SalesFinder category IDs by marketplace, date period and optional category name."""
    take = max(1, min(int(take), 100))
    skip = max(0, int(skip))
    body: dict[str, Any] = {
        "marketPlace": marketplace,
        "dateFrom": date_from,
        "dateTo": date_to,
        "take": take,
        "skip": skip,
    }
    if category_name:
        body["categoryName"] = category_name
    return await sf.post("ext-analitic/show_category", body)


@mcp.tool()
async def create_category_report(
    marketplace: Literal["wb", "ozon"],
    category_id: int,
    date_from: str,
    date_to: str,
    include_fbs: bool = False,
) -> Any:
    """Create an asynchronous category report and return its reportId.

    After this call, use category_overview/category_products with the returned reportId.
    """
    return await sf.post(
        "ext-analitic/category_report",
        {
            "marketPlace": marketplace,
            "category": int(category_id),
            "dateFrom": date_from,
            "dateTo": date_to,
            "fbs": bool(include_fbs),
        },
    )


@mcp.tool()
async def category_overview(report_id: str, filters: Optional[dict[str, Any]] = None) -> Any:
    """Get category-level overview metrics for a previously created reportId."""
    body: dict[str, Any] = {"reportId": report_id}
    if filters:
        body["filter"] = filters
    return await sf.post("ext-analitic/category_overview_all", body)


@mcp.tool()
async def category_products(
    report_id: str,
    take: int = 20,
    skip: int = 0,
    sort: Optional[str] = "revenue",
    sort_direction: Literal["asc", "desc"] = "desc",
    filters: Optional[dict[str, Any]] = None,
) -> Any:
    """Get products from a category report with pagination, sorting and SalesFinder filters.

    Useful filters include revenue, sold, price, remains, rating, p_reviews, brand and seller.
    Range filters generally use objects like {"min": 1000000, "max": 5000000}.
    """
    body: dict[str, Any] = {
        "reportId": report_id,
        "take": max(1, min(int(take), 100)),
        "skip": max(0, int(skip)),
        "sort_dir": sort_direction,
    }
    if sort:
        body["sort"] = sort
    if filters:
        body["filter"] = filters
    return await sf.post("ext-analitic/get_category_product", body)


async def health(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "mode": "claude-remote-mcp",
            "salesfinder_api_version": SALESFINDER_API_VERSION,
            "salesfinder_credentials_configured": bool(SALESFINDER_EMAIL and SALESFINDER_PASSWORD),
            "mcp_secret_configured": bool(MCP_URL_SECRET),
        }
    )


# Render already terminates HTTPS/reverse-proxies the app, so the MCP SDK docs allow
# DNS-rebinding protection to be disabled behind that controlled proxy.
transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# Mount MCP at a secret, unguessable URL. Claude stores the full URL as the connector URL.
mcp_app = mcp.streamable_http_app(
    streamable_http_path="/",
    json_response=True,
    stateless_http=True,
    transport_security=transport_security,
)

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_: Starlette):
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount(f"/mcp/{MCP_URL_SECRET}", app=mcp_app),
    ],
    lifespan=lifespan,
)
