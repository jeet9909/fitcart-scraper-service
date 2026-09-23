import os
from io import BytesIO
from datetime import UTC, datetime

from PIL import Image

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("BRIGHTDATA_API_TOKEN", "test")
for proxy_variable in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(proxy_variable, None)

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_runtime_settings, get_scraper, get_tryon_service
from app.models import Money, ProductData, ScrapeResponse
from app.scraper import ScrapeProviderError
from app.scraper import _strip_security_wrapper
from app.tryon import TryOnService


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


class InvalidShareLinkScraper:
    async def scrape(self, url: str, country: str) -> ScrapeResponse:
        raise ScrapeProviderError("The product share URL is invalid", code="invalid_share_link")


def test_health() -> None:
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_storefront_serves_approved_ui() -> None:
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Your next find" in response.text
    assert "static/config.js" in response.text
    assert "static/api.js" in response.text


def test_live_ui_adapter_is_served() -> None:
    with TestClient(app) as client:
        response = client.get("/static/api.js")
    assert response.status_code == 200
    assert "/v1/products/scrape" in response.text
    assert "/v1/try-ons" in response.text


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


def test_empty_allowlist_accepts_any_public_host() -> None:
    assert Settings(brightdata_api_token="test", ALLOWED_PRODUCT_HOSTS="").allowed_product_hosts == ()
    configured = Settings(brightdata_api_token="test", ALLOWED_PRODUCT_HOSTS="amazon.in").allowed_product_hosts
    assert configured == ("amazon.in", "amzn.in", "fkrt.it")


def test_security_wrapper_is_removed() -> None:
    wrapped = "SECURITY NOTICE\n=====UNTRUSTED_abc123_BEGIN=====\n# Product title\n₹599\n=====UNTRUSTED_abc123_END====="
    assert _strip_security_wrapper(wrapped) == "# Product title\n₹599"


def test_invalid_share_link_becomes_bad_request(monkeypatch) -> None:
    monkeypatch.setattr("app.security.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("13.32.151.88", 0))])
    app.dependency_overrides[get_runtime_settings] = lambda: SETTINGS
    app.dependency_overrides[get_scraper] = lambda: InvalidShareLinkScraper()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/products/scrape",
                json={"url": "https://amzn.in/d/expired", "country": "IN"},
            )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_share_link"
    finally:
        app.dependency_overrides.clear()


def test_anonymous_session_returns_signed_token() -> None:
    session_settings = Settings(
        openai_api_key="test",
        brightdata_api_token="test",
        anonymous_token_secret="a-secure-test-secret-that-is-long-enough",
    )
    app.dependency_overrides[get_runtime_settings] = lambda: session_settings
    app.dependency_overrides[get_tryon_service] = lambda: TryOnService(session_settings)
    try:
        with TestClient(app) as client:
            response = client.post("/v1/sessions/anonymous")
        assert response.status_code == 200
        assert response.json()["token_type"] == "bearer"
        assert response.json()["anonymous_user_id"]
        assert response.json()["access_token"]
    finally:
        app.dependency_overrides.clear()


def test_gallery_requires_bearer_token() -> None:
    app.dependency_overrides[get_runtime_settings] = lambda: SETTINGS
    try:
        with TestClient(app) as client:
            response = client.get("/v1/gallery")
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_swagger_double_bearer_format_is_accepted() -> None:
    session_settings = Settings(
        openai_api_key="test",
        brightdata_api_token="test",
        anonymous_token_secret="a-secure-test-secret-that-is-long-enough",
    )
    app.dependency_overrides[get_runtime_settings] = lambda: session_settings
    try:
        with TestClient(app) as client:
            session = client.post("/v1/sessions/anonymous").json()
            response = client.get(
                "/v1/gallery",
                headers={"Authorization": f"Bearer Bearer {session['access_token']}"},
            )
        # Authentication succeeded; gallery configuration is intentionally absent.
        assert response.status_code == 503
        assert response.json()["detail"].startswith("Try-on service is not configured")
    finally:
        app.dependency_overrides.clear()


def test_tryon_rejects_non_image_upload() -> None:
    session_settings = Settings(
        openai_api_key="test",
        brightdata_api_token="test",
        gemini_api_key="test",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="test",
        anonymous_token_secret="a-secure-test-secret-that-is-long-enough",
    )
    app.dependency_overrides[get_runtime_settings] = lambda: session_settings
    app.dependency_overrides[get_tryon_service] = lambda: TryOnService(session_settings)
    try:
        with TestClient(app) as client:
            session = client.post("/v1/sessions/anonymous").json()
            response = client.post(
                "/v1/try-ons",
                headers={"Authorization": f"Bearer {session['access_token']}"},
                files={
                    "person_image": ("person.txt", b"not an image", "text/plain"),
                    "product_image": ("product.png", _tiny_png(), "image/png"),
                },
                data={"category": "shirt"},
            )
        assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()


def _tiny_png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_amazon_images_found_outside_markdown_image_syntax() -> None:
    from app.scraper import _parse_product

    markdown = """# Men's Cotton Shirt
[Visit the Brand Store](https://www.amazon.in/stores/brand)
![](https://m.media-amazon.com/images/G/31/nav-sprite-global-1x.png)
[Image: https://m.media-amazon.com/images/I/71abcXYZ._SY879_.jpg](https://www.amazon.in/dp/B0TEST)
₹799 M.R.P.: ₹1,999
"""
    product = _parse_product(markdown, "https://www.amazon.in/dp/B0TEST")
    assert product.image_urls == ["https://m.media-amazon.com/images/I/71abcXYZ._SY879_.jpg"]


def test_myntra_templated_images_are_expanded() -> None:
    from app.scraper import _parse_product

    markdown = """# Roadster Men Checked Shirt
![Roadster](http://assets.myntassets.com/h_($height),q_($qualityPercentage),w_($width)/v1/assets/images/123/2024/shirt-1.jpg)
Rs. 699
"""
    product = _parse_product(markdown, "https://www.myntra.com/shirts/roadster/123/buy")
    assert product.image_urls == [
        "https://assets.myntassets.com/h_1440,q_90,w_1080/v1/assets/images/123/2024/shirt-1.jpg"
    ]


def test_images_are_read_from_html_metadata() -> None:
    from app.scraper import _images_from_html

    page = """<html><head>
<meta property="og:image" content="https://assets.myntassets.com/v1/assets/images/1/og.jpg">
<script type="application/ld+json">{"@type":"Product","image":["https://assets.myntassets.com/v1/assets/images/1/ld.jpg"]}</script>
</head><body><img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/81hires.jpg"
data-a-dynamic-image="{&quot;https://m.media-amazon.com/images/I/81dyn._SX679_.jpg&quot;:[679,679]}">
<img src="/images/logo.svg"></body></html>"""
    images = _images_from_html(page, "https://www.example.com/p")
    assert images[:4] == [
        "https://assets.myntassets.com/v1/assets/images/1/og.jpg",
        "https://assets.myntassets.com/v1/assets/images/1/ld.jpg",
        "https://m.media-amazon.com/images/I/81hires.jpg",
        "https://m.media-amazon.com/images/I/81dyn._SX679_.jpg",
    ]


def test_scraper_falls_back_to_html_when_markdown_has_no_images() -> None:
    import asyncio

    from app.scraper import BrightDataScraper

    class StubScraper(BrightDataScraper):
        async def _fetch_page(self, url: str) -> str:
            return "# Linen Shirt\n₹1,299\nAdd to cart"

        async def _fetch_html_via_unlocker(self, url: str) -> str | None:
            return None

        async def _fetch_html_directly(self, url: str) -> str | None:
            return '<meta property="og:image" content="https://m.media-amazon.com/images/I/61shirt.jpg">'

    result = asyncio.run(StubScraper(SETTINGS).scrape("https://www.amazon.in/dp/B0TEST", "IN"))
    assert result.data.image_urls == ["https://m.media-amazon.com/images/I/61shirt.jpg"]
    assert result.data.price.amount == 1299


def test_scraper_uses_unlocker_html_before_direct_fetch() -> None:
    import asyncio

    from app.scraper import BrightDataScraper

    class StubScraper(BrightDataScraper):
        async def _fetch_page(self, url: str) -> str:
            return "# Roadster Men Shirt\nRs. 699"

        async def _fetch_html_via_unlocker(self, url: str) -> str | None:
            return (
                '<script>window.__myx = {"images":[{"src":"http:\\/\\/assets.myntassets.com\\/'
                'h_($height),q_($qualityPercentage),w_($width)\\/v1\\/assets\\/images\\/9\\/shirt.jpg"}]}</script>'
            )

        async def _fetch_html_directly(self, url: str) -> str | None:
            raise AssertionError("direct fetch should not run")

    result = asyncio.run(StubScraper(SETTINGS).scrape("https://www.myntra.com/shirts/roadster/9/buy", "IN"))
    assert result.data.image_urls == ["https://assets.myntassets.com/h_1440,q_90,w_1080/v1/assets/images/9/shirt.jpg"]


def test_amazon_captcha_image_is_not_a_product_image() -> None:
    from app.scraper import _images_from_html

    page = '<img src="https://images-na.ssl-images-amazon.com/captcha/abc/Captcha_xyz.jpg">'
    assert _images_from_html(page, "https://www.amazon.in/dp/B0TEST") == []


def test_tryon_reuses_scraped_image_without_scraping_again() -> None:
    from app.models import GalleryItem

    session_settings = Settings(
        brightdata_api_token="test",
        anonymous_token_secret="a-secure-test-secret-that-is-long-enough",
        ALLOWED_PRODUCT_HOSTS="example.com",
    )
    fetched: list[str] = []
    saved: dict[str, object] = {}

    class NoScrape:
        async def scrape(self, url: str, country: str) -> ScrapeResponse:
            raise AssertionError("the product page must not be scraped again")

    class StubTryOn:
        def ensure_configured(self) -> None:
            pass

        async def fetch_image(self, url: str) -> tuple[bytes, str, str]:
            fetched.append(url)
            return _tiny_png(), "image/png", "png"

        async def generate(self, person, product, category):
            return _tiny_png(), "image/png", "png"

        async def save(self, user_id, person, product, result, category, product_source, product_url) -> GalleryItem:
            saved.update(product_source=product_source, product_url=product_url)
            return GalleryItem(
                id="1", anonymous_user_id=user_id, category=category, product_source=product_source,
                product_url=product_url, person_image_url="https://example.com/p.png",
                product_image_url="https://example.com/i.png", result_image_url="https://example.com/r.png",
                model="test", created_at=datetime(2026, 9, 23, tzinfo=UTC),
            )

    app.dependency_overrides[get_runtime_settings] = lambda: session_settings
    app.dependency_overrides[get_scraper] = lambda: NoScrape()
    app.dependency_overrides[get_tryon_service] = lambda: StubTryOn()
    try:
        with TestClient(app) as client:
            session = client.post("/v1/sessions/anonymous").json()
            response = client.post(
                "/v1/try-ons",
                headers={"Authorization": f"Bearer {session['access_token']}"},
                files={"person_image": ("person.png", _tiny_png(), "image/png")},
                data={
                    "category": "top",
                    "product_image_url": "https://m.media-amazon.com/images/I/61shirt.jpg",
                    "product_page_url": "https://www.example.com/p/shirt",
                },
            )
        assert response.status_code == 200, response.text
        assert fetched == ["https://m.media-amazon.com/images/I/61shirt.jpg"]
        assert saved == {"product_source": "image_url", "product_url": "https://www.example.com/p/shirt"}
    finally:
        app.dependency_overrides.clear()


def test_gemini_usage_requires_admin_token() -> None:
    admin_settings = Settings(brightdata_api_token="test", admin_api_token="admin-secret")
    app.dependency_overrides[get_runtime_settings] = lambda: admin_settings
    try:
        with TestClient(app) as client:
            assert client.get("/v1/admin/gemini/usage").status_code == 401
            assert client.get("/v1/admin/gemini/usage", headers={"X-Admin-Token": "wrong"}).status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_gemini_usage_reports_key_check_and_counters() -> None:
    admin_settings = Settings(brightdata_api_token="test", admin_api_token="admin-secret")
    service = TryOnService(admin_settings)
    service.usage.requests, service.usage.succeeded, service.usage.total_tokens = 3, 2, 4500
    app.dependency_overrides[get_runtime_settings] = lambda: admin_settings
    app.dependency_overrides[get_tryon_service] = lambda: service
    try:
        with TestClient(app) as client:
            response = client.get("/v1/admin/gemini/usage", headers={"X-Admin-Token": "admin-secret"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    body = response.json()
    assert body["key_valid"] is False
    assert body["check_message"] == "GEMINI_API_KEY is not configured"
    assert body["remaining_credits"] is None
    assert body["since_server_start"]["requests"] == 3
    assert body["since_server_start"]["total_tokens"] == 4500
