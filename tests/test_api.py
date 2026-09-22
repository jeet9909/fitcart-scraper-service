import os
from datetime import UTC, datetime

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("BRIGHTDATA_API_TOKEN", "test")
for proxy_variable in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(proxy_variable, None)

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_runtime_settings, get_scraper
from app.models import Money, ProductData, ScrapeResponse
from app.scraper import ScrapeProviderError
from app.scraper import _strip_security_wrapper


SETTINGS = Settings(
    openai_api_key="test",
    brightdata_api_token="test",
    ALLOWED_PRODUCT_HOSTS="example.com",
)


class FakeScraper:
    async def scrape(self, url: str, country: str) -> ScrapeResponse:
        return ScrapeResponse(
            data=ProductData(
                source_url=url,
                store="Example",
                title="Green Shirt",
                price=Money(amount=999, currency="INR"),
                image_urls=["https://example.com/shirt.jpg"],
            ),
            scraped_at=datetime(2026, 9, 22, tzinfo=UTC),
        )


class FailingScraper:
    async def scrape(self, url: str, country: str) -> ScrapeResponse:
        raise ScrapeProviderError("Product scraping failed")


def test_health() -> None:
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_scrape_returns_normalized_product(monkeypatch) -> None:
    monkeypatch.setattr("app.security.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("93.184.216.34", 0))])
    app.dependency_overrides[get_runtime_settings] = lambda: SETTINGS
    app.dependency_overrides[get_scraper] = lambda: FakeScraper()
    try:
        with TestClient(app) as client:
            response = client.post("/v1/products/scrape", json={"url": "https://example.com/product/1", "country": "IN"})
        assert response.status_code == 200
        assert response.json()["data"]["price"] == {"amount": 999.0, "currency": "INR"}
    finally:
        app.dependency_overrides.clear()


def test_private_url_is_rejected() -> None:
    app.dependency_overrides[get_runtime_settings] = lambda: SETTINGS
    app.dependency_overrides[get_scraper] = lambda: FakeScraper()
    try:
        with TestClient(app) as client:
            response = client.post("/v1/products/scrape", json={"url": "http://127.0.0.1/product", "country": "IN"})
        assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_provider_failure_becomes_bad_gateway(monkeypatch) -> None:
    monkeypatch.setattr("app.security.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("93.184.216.34", 0))])
    app.dependency_overrides[get_runtime_settings] = lambda: SETTINGS
    app.dependency_overrides[get_scraper] = lambda: FailingScraper()
    try:
        with TestClient(app) as client:
            response = client.post("/v1/products/scrape", json={"url": "https://example.com/product/1", "country": "IN"})
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "provider_failed"
    finally:
        app.dependency_overrides.clear()


def test_amazon_share_url_is_accepted_with_configured_allowlist(monkeypatch) -> None:
    monkeypatch.setattr("app.security.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("13.32.151.88", 0))])
    restricted_settings = Settings(
        openai_api_key="test",
        brightdata_api_token="test",
        ALLOWED_PRODUCT_HOSTS="amazon.in,myntra.com",
    )
    app.dependency_overrides[get_runtime_settings] = lambda: restricted_settings
    app.dependency_overrides[get_scraper] = lambda: FakeScraper()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/products/scrape",
                json={"url": "https://amzn.in/d/OdACsne", "country": "IN"},
            )
        assert response.status_code == 200
        assert response.json()["data"]["source_url"] == "https://amzn.in/d/OdACsne"
    finally:
        app.dependency_overrides.clear()


def test_security_wrapper_is_removed() -> None:
    wrapped = "SECURITY NOTICE\n=====UNTRUSTED_abc123_BEGIN=====\n# Product title\n₹599\n=====UNTRUSTED_abc123_END====="
    assert _strip_security_wrapper(wrapped) == "# Product title\n₹599"
