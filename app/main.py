from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status

from app.config import Settings, get_settings
from app.models import HealthResponse, ScrapeRequest, ScrapeResponse
from app.scraper import BrightDataScraper, ScrapeProviderError
from app.security import UnsafeUrlError, validate_public_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.scraper = BrightDataScraper(settings)
    yield


app = FastAPI(
    title="FitCart Scraper Service",
    version="0.1.0",
    description="Fetch normalized product details through Bright Data MCP.",
    lifespan=lifespan,
)


def get_scraper(request: Request) -> BrightDataScraper:
    return request.app.state.scraper


def get_runtime_settings(request: Request) -> Settings:
    return request.app.state.settings


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    return HealthResponse()


@app.get("/ready", response_model=HealthResponse, tags=["system"])
async def ready(settings: Settings = Depends(get_runtime_settings)) -> HealthResponse:
    if not settings.openai_api_key.get_secret_value() or not settings.brightdata_api_token.get_secret_value():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service is not configured")
    return HealthResponse()


@app.post("/v1/products/scrape", response_model=ScrapeResponse, tags=["products"])
async def scrape_product(
    payload: ScrapeRequest,
    scraper: BrightDataScraper = Depends(get_scraper),
    settings: Settings = Depends(get_runtime_settings),
) -> ScrapeResponse:
    try:
        url = validate_public_url(str(payload.url), settings.allowed_product_hosts)
        return await scraper.scrape(url, payload.country)
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ScrapeProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

