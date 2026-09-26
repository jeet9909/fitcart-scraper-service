import json
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
    assert "FitCart" in response.text
    assert "static/config.js" in response.text
    assert "static/app.js" in response.text


def test_live_ui_adapter_is_served() -> None:
    with TestClient(app) as client:
        response = client.get("/static/app.js")
        image = client.get("/static/img/after.jpg")
    assert response.status_code == 200
    for endpoint in ("/v1/products/scrape", "/v1/try-ons", "/v1/wardrobe", "/v1/wardrobe/suggestions", "/v1/gallery"):
        assert endpoint in response.text
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"


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
        look_limits_enabled=False,
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

        async def generate(self, person, product, category, product_name=None, pose="standard"):
            return _tiny_png(), "image/png", "png"

        async def save(self, user_id, person, product, result, category, product_source, product_url, items=None) -> GalleryItem:
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


def _gemini_quota_service(monkeypatch, responses: list[dict]) -> tuple:
    import asyncio as _asyncio
    import httpx as _httpx

    calls: list[int] = []

    def handler(request: _httpx.Request) -> _httpx.Response:
        calls.append(1)
        return _httpx.Response(429, json=responses[min(len(calls), len(responses)) - 1])

    real_client = _httpx.AsyncClient
    monkeypatch.setattr("app.tryon.httpx.AsyncClient", lambda **kwargs: real_client(transport=_httpx.MockTransport(handler), **kwargs))
    async def no_sleep(_seconds: float) -> None: return None
    monkeypatch.setattr("app.tryon.asyncio.sleep", no_sleep)
    service = TryOnService(Settings(gemini_api_key="test"))
    image = (_tiny_png(), "image/png", "png")
    service_call = lambda: _asyncio.run(service.generate(image, image, "shirt"))
    return service, calls, service_call


def _quota_error(quota_id: str, value: str, retry: str | None = None) -> dict:
    details = [{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests", "quotaId": quota_id, "quotaDimensions": {"model": "gemini-2.5-flash-image"}, "quotaValue": value}],
    }]
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return {"error": {"code": 429, "message": "You exceeded your current quota", "status": "RESOURCE_EXHAUSTED", "details": details}}


def test_gemini_zero_quota_explains_billing_without_retrying(monkeypatch) -> None:
    from app.tryon import TryOnError
    service, calls, generate = _gemini_quota_service(monkeypatch, [_quota_error("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "0", "30s")])
    try:
        generate()
        raise AssertionError("expected TryOnError")
    except TryOnError as exc:
        assert exc.status_code == 429
        assert "enable billing" in str(exc)
    assert len(calls) == 1


def test_gemini_short_rate_limit_is_retried_once(monkeypatch) -> None:
    from app.tryon import TryOnError
    service, calls, generate = _gemini_quota_service(monkeypatch, [_quota_error("GenerateRequestsPerMinutePerProjectPerModel", "10", "5s")])
    try:
        generate()
        raise AssertionError("expected TryOnError")
    except TryOnError as exc:
        assert exc.status_code == 429
        assert "try again in about 5 seconds" in str(exc)
    assert len(calls) == 2


def test_gemini_daily_quota_message(monkeypatch) -> None:
    from app.tryon import TryOnError
    service, calls, generate = _gemini_quota_service(monkeypatch, [_quota_error("GenerateRequestsPerDayPerProjectPerModel", "100")])
    try:
        generate()
        raise AssertionError("expected TryOnError")
    except TryOnError as exc:
        assert "daily Gemini quota" in str(exc)
    assert len(calls) == 1


MYNTRA_PAGE = """<html><head><title>Buy Allen Solly Men Slim Fit Formal Trousers - Trousers for Men 42057155 | Myntra</title>
<script type="application/ld+json">{"@context":"http://schema.org/","@type":"Product","name":"Allen Solly Men Slim Fit Formal Trousers",
"offers":{"@type":"Offer","priceCurrency":"INR","price":"1471","availability":"http://schema.org/InStock"}}</script></head>
<body><script>window.__myx = {"pdpData":{"id":42057155,"name":"Allen Solly Men Slim Fit Formal Trousers","mrp":2299,
"price":{"mrp":2299,"discounted":1471},"brand":{"name":"Allen Solly"},"baseColour":"Beige",
"analytics":{"articleType":"Trousers","gender":"Men"},"articleAttributes":{"Fabric":"Cotton Blend","Fit":"Slim Fit"},
"sizes":[{"label":"28","available":false},{"label":"30","available":true},{"label":"32","available":true},{"label":"34","available":true}],
"ratings":{"averageRating":4.4,"totalCount":30},
"media":{"albums":[{"name":"default","images":[{"imageURL":"http://assets.myntassets.com/h_($height),q_($qualityPercentage),w_($width)/v1/assets/images/42057155/trousers.jpg"}]}]},
"productDetails":[{"title":"Product Details","description":"Beige solid <b>slim fit</b> formal trousers"}]}};</script>
<div>Similar products ₹599</div></body></html>"""


def test_myntra_page_data_gives_real_price_sizes_and_colour() -> None:
    from app.scraper import _structured_product

    data = _structured_product(MYNTRA_PAGE, "https://www.myntra.com/trousers/allen-solly/42057155/buy")
    assert data["title"] == "Allen Solly Men Slim Fit Formal Trousers"
    assert data["price"] == 1471 and data["mrp"] == 2299
    assert data["sizes"] == ["30", "32", "34"] and data["unavailable_sizes"] == ["28"]
    assert data["color"] == "Beige" and data["material"] == "Cotton Blend" and data["brand"] == "Allen Solly"
    assert data["rating"] == 4.4 and data["review_count"] == 30


def test_scraper_prefers_structured_html_and_skips_markdown() -> None:
    import asyncio

    from app.scraper import BrightDataScraper

    class StubScraper(BrightDataScraper):
        async def _fetch_page(self, url: str) -> str:
            raise AssertionError("markdown is not needed when the HTML has complete product data")

        async def _fetch_html_via_unlocker(self, url: str) -> str | None:
            return MYNTRA_PAGE

    product = asyncio.run(StubScraper(SETTINGS).scrape("https://www.myntra.com/trousers/allen-solly/42057155/buy", "IN")).data
    assert product.price.amount == 1471 and product.original_price.amount == 2299
    assert product.discount_percent == 36.02
    assert product.sizes == ["30", "32", "34"] and product.unavailable_sizes == ["28"]
    assert product.colors == ["Beige"] and product.outfit_slot == "bottom"
    assert product.image_urls[0] == "https://assets.myntassets.com/h_1440,q_90,w_1080/v1/assets/images/42057155/trousers.jpg"


def test_amazon_html_price_sizes_and_brand() -> None:
    from app.scraper import _structured_product

    page = """<span id="productTitle" class="a-size-large">  Levi's Men's Slim Fit T-Shirt  </span>
<a id="bylineInfo" href="/stores/Levis">Visit the Levi's Store</a>
<div id="corePriceDisplay_desktop_feature_div"><span class="a-price aok-align-center reinventPricePriceToPayMargin priceToPay"><span class="a-offscreen">₹649.00</span></span>
<span class="a-price a-text-price" data-a-strike="true"><span class="a-offscreen">₹1,299.00</span></span></div><div id="deliveryBlock"></div>
<script>var data = {"variationValues" : {"size_name":["S","M","L","XL"],"color_name":["Black","White"]}};</script>"""
    data = _structured_product(page, "https://www.amazon.in/dp/B0TEST")
    assert data["title"] == "Levi's Men's Slim Fit T-Shirt"
    assert data["brand"] == "Levi's"
    assert data["price"] == 649 and data["mrp"] == 1299
    assert data["sizes"] == ["S", "M", "L", "XL"]
    assert "colors" not in data  # two colour variants and no current one: do not guess


def test_markdown_price_uses_mrp_pair_not_first_rupee_amount() -> None:
    from app.scraper import _parse_product

    markdown = "Free delivery above ₹599\n# Allen Solly Men Slim Fit Formal Trousers\n₹1471 MRP ₹2299 (36% OFF)\n"
    product = _parse_product(markdown, "https://www.myntra.com/42057155")
    assert product.price.amount == 1471
    assert product.original_price.amount == 2299


def test_titles_are_cleaned_and_slots_detected() -> None:
    from app.scraper import _clean_title, outfit_slot

    assert _clean_title("Buy Allen Solly Men Slim Fit Formal Trousers - Trousers for Men 42057155 | Myntra") == "Allen Solly Men Slim Fit Formal Trousers"
    assert _clean_title("Amazon.in: Puma Unisex Sneakers") == "Puma Unisex Sneakers"
    assert outfit_slot("Casual Shoes") == "footwear"
    assert outfit_slot("Kurtas") == "top"
    assert outfit_slot("Gold-Plated Earrings") == "jewelry"
    assert outfit_slot(None, "Men Slim Fit Jeans") == "bottom"


def test_outfit_prompt_sends_every_piece(monkeypatch) -> None:
    import asyncio
    import base64
    import json as _json

    import httpx as _httpx

    from app.tryon import OutfitPiece

    sent: dict = {}

    def handler(request: _httpx.Request) -> _httpx.Response:
        sent.update(_json.loads(request.content))
        image = base64.b64encode(_tiny_png()).decode()
        return _httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": image}}]}}]})

    real_client = _httpx.AsyncClient
    monkeypatch.setattr("app.tryon.httpx.AsyncClient", lambda **kwargs: real_client(transport=_httpx.MockTransport(handler), **kwargs))
    service = TryOnService(Settings(gemini_api_key="test"))
    image = (_tiny_png(), "image/png", "png")
    pieces = [OutfitPiece(image, "top", "white shirt"), OutfitPiece(image, "bottom wear", "beige trousers"), OutfitPiece(image, "footwear")]
    result = asyncio.run(service.generate_outfit(image, pieces))
    parts = sent["contents"][0]["parts"]
    assert len(parts) == 5
    assert "image 3 is the bottom wear (beige trousers)" in parts[0]["text"]
    assert result[1] == "image/png"


def test_outfit_suggestions_drop_unknown_items() -> None:
    import asyncio

    from app.wardrobe import WardrobeService

    rows = [
        {"id": "11111111-1111-1111-1111-111111111111", "slot": "top", "name": "White shirt", "collection": "home", "image_path": "a"},
        {"id": "22222222-2222-2222-2222-222222222222", "slot": "bottom", "name": "Beige trousers", "collection": "store", "price": 1471, "image_path": "b"},
    ]

    class StubStorage:
        async def download(self, path):
            return _tiny_png(), "image/png", "png"

        async def generate_json(self, parts, schema):
            assert sum("inline_data" in part for part in parts) == 2
            return {"outfits": [
                {"title": "Smart casual", "reason": "Neutral tones", "item_ids": [rows[0]["id"], rows[1]["id"], "made-up"]},
                {"title": "Only one real item", "reason": "", "item_ids": [rows[0]["id"]]},
            ]}

    service = WardrobeService(StubStorage())

    async def fake_rows(user_id, collection=None, ids=None):
        return rows

    service._rows = fake_rows
    outfits = asyncio.run(service.suggest("user", "all", "office", 3))
    assert [outfit.title for outfit in outfits] == ["Smart casual"]
    assert outfits[0].item_ids == [rows[0]["id"], rows[1]["id"]]


def test_outfit_tryon_endpoint_uses_wardrobe_items() -> None:
    from app.main import get_wardrobe_service
    from app.models import GalleryItem
    from app.tryon import OutfitPiece

    session_settings = Settings(brightdata_api_token="test", anonymous_token_secret="a-secure-test-secret-that-is-long-enough")
    calls: dict = {}

    class StubWardrobe:
        async def outfit_pieces(self, user_id, item_ids):
            calls["item_ids"] = item_ids
            image = (_tiny_png(), "image/png", "png")
            return [OutfitPiece(image, "top"), OutfitPiece(image, "footwear")], [
                {"id": item_ids[0], "slot": "top", "name": "Shirt", "collection": "home", "product_url": None},
                {"id": item_ids[1], "slot": "footwear", "name": "Sneakers", "collection": "store", "product_url": "https://www.example.com/shoe"},
            ]

    class StubTryOn:
        def ensure_configured(self) -> None:
            pass

        async def generate_outfit(self, person, pieces, pose="standard"):
            calls["pieces"] = len(pieces)
            return _tiny_png(), "image/png", "png"

        async def save(self, user_id, person, product, result, category, product_source, product_url, items=None) -> GalleryItem:
            calls.update(category=category, product_source=product_source, product_url=product_url)
            return GalleryItem(
                id="1", anonymous_user_id=user_id, category=category, product_source=product_source, product_url=product_url,
                person_image_url="https://example.com/p.png", product_image_url="https://example.com/i.png",
                result_image_url="https://example.com/r.png", model="test", items=items or [], created_at=datetime(2026, 9, 23, tzinfo=UTC),
            )

    app.dependency_overrides[get_runtime_settings] = lambda: session_settings
    app.dependency_overrides[get_tryon_service] = lambda: StubTryOn()
    app.dependency_overrides[get_wardrobe_service] = lambda: StubWardrobe()
    try:
        with TestClient(app) as client:
            session = client.post("/v1/sessions/anonymous").json()
            response = client.post(
                "/v1/try-ons/outfit",
                headers={"Authorization": f"Bearer {session['access_token']}"},
                files={"person_image": ("person.png", _tiny_png(), "image/png")},
                data={"item_ids": "a, b"},
            )
        assert response.status_code == 200, response.text
        assert calls == {"item_ids": ["a", "b"], "pieces": 2, "category": "top + footwear", "product_source": "wardrobe", "product_url": "https://www.example.com/shoe"}
        assert len(response.json()["items"]) == 2
    finally:
        app.dependency_overrides.clear()


def test_brand_words_do_not_decide_the_outfit_slot() -> None:
    from app.scraper import outfit_slot

    assert outfit_slot(None, "Calvin Klein Jeans Men Shirt", brand="Calvin Klein Jeans") == "top"
    assert outfit_slot(None, "Calvin Klein Jeans Men Shirt") == "top"
    assert outfit_slot(None, "Men Slim Fit Denim Jacket") == "outerwear"
    assert outfit_slot(None, "Women Kurta Set") == "dress"
    assert outfit_slot(None, "Levi's Men 511 Slim Fit Jeans") == "bottom"


def test_amazon_sizes_missing_or_marked_unavailable_are_sold_out() -> None:
    from app.scraper import _structured_product

    page = """<span id="productTitle">Calvin Klein Jeans Men Shirt</span>
<a id="bylineInfo">Brand: Calvin Klein Jeans</a>
<script>var twister = {"currentAsin" : "B0SHIRTM01", "dimensions" : ["size_name","color_name"],
"variationValues" : {"size_name":["S","M","L","XL","2XL"],"color_name":["CK BLACK","CK NAVY"]},
"dimensionValuesDisplayData" : {"B0SHIRTS01":["S","CK BLACK"],"B0SHIRTM01":["M","CK BLACK"],"B0SHIRTL01":["L","CK BLACK"],
"B0SHIRTX01":["XL","CK BLACK"],"B0SHIRT2N1":["2XL","CK NAVY"],"B0SHIRTMN1":["M","CK NAVY"]}};</script>
<ul><li id="size_name_3" class="swatchUnavailable" title="Click to select XL"><span class="a-button-text">XL</span></li>
<li id="size_name_1" class="swatchSelect" title="Click to select M"><span>M</span></li></ul>"""
    data = _structured_product(page, "https://www.amazon.in/dp/B0SHIRTM01")
    assert data["sizes"] == ["S", "M", "L"]
    assert data["unavailable_sizes"] == ["XL", "2XL"]
    assert data["colors"] == ["CK BLACK"]
    assert data["brand"] == "Calvin Klein Jeans"


def test_tryon_with_extra_pieces_from_other_stores() -> None:
    import json as _json

    from app.models import GalleryItem

    session_settings = Settings(brightdata_api_token="test", anonymous_token_secret="a-secure-test-secret-that-is-long-enough", ALLOWED_PRODUCT_HOSTS="")
    calls: dict = {"fetched": []}

    class StubTryOn:
        def ensure_configured(self) -> None:
            pass

        async def fetch_image(self, url: str):
            calls["fetched"].append(url)
            return _tiny_png(), "image/png", "png"

        async def generate_outfit(self, person, pieces, pose="standard"):
            calls["pose"] = pose
            calls["pieces"] = [(piece.category, piece.label) for piece in pieces]
            return _tiny_png(), "image/png", "png"

        async def save(self, user_id, person, product, result, category, product_source, product_url, items=None) -> GalleryItem:
            calls.update(category=category, items=items)
            return GalleryItem(
                id="1", anonymous_user_id=user_id, category=category, product_source=product_source, product_url=product_url,
                person_image_url="https://example.com/p.png", product_image_url="https://example.com/i.png",
                result_image_url="https://example.com/r.png", model="test", items=items or [], created_at=datetime(2026, 9, 23, tzinfo=UTC),
            )

    app.dependency_overrides[get_runtime_settings] = lambda: session_settings
    app.dependency_overrides[get_tryon_service] = lambda: StubTryOn()
    extras = [
        {"slot": "bottom", "name": "Beige chinos", "image_url": "https://m.media-amazon.com/images/I/chinos.jpg", "page_url": "https://www.amazon.in/dp/B0CHINO", "store": "amazon.in", "price": 1299, "size": "32"},
        {"slot": "footwear", "name": "White sneakers", "upload": 0},
    ]
    try:
        with TestClient(app) as client:
            token = client.post("/v1/sessions/anonymous").json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            response = client.post(
                "/v1/try-ons",
                headers=headers,
                files=[("person_image", ("p.png", _tiny_png(), "image/png")), ("outfit_images", ("shoe.png", _tiny_png(), "image/png"))],
                data={"category": "top", "product_name": "CK BLACK Calvin Klein Jeans Men Shirt", "product_image_url": "https://assets.myntassets.com/shirt.jpg",
                      "product_page_url": "https://www.myntra.com/shirt/1", "outfit_items": _json.dumps(extras)},
            )
            invalid = client.post(
                "/v1/try-ons",
                headers=headers,
                files={"person_image": ("p.png", _tiny_png(), "image/png")},
                data={"product_image_url": "https://assets.myntassets.com/shirt.jpg", "outfit_items": _json.dumps([{"slot": "footwear", "upload": 0}])},
            )
        assert response.status_code == 200, response.text
        assert calls["pieces"] == [("top", "CK BLACK Calvin Klein Jeans Men Shirt"), ("bottom wear", "Beige chinos"), ("footwear", "White sneakers")]
        assert calls["fetched"] == ["https://assets.myntassets.com/shirt.jpg", "https://m.media-amazon.com/images/I/chinos.jpg"]
        assert calls["category"] == "top + bottom + footwear"
        assert calls["items"][1]["product_url"] == "https://www.amazon.in/dp/B0CHINO"
        assert invalid.status_code == 400 and "not uploaded" in invalid.json()["detail"]
    finally:
        app.dependency_overrides.clear()


AMAZON_SHIRT_PAGE = """<html><body>
<div id="sponsored"><img data-a-dynamic-image="{&quot;https://m.media-amazon.com/images/I/TSHIRT_SPONSORED._AC_.jpg&quot;:[300,300]}"></div>
<span id="productTitle">Calvin Klein Jeans Men Shirt</span>
<div id="corePriceDisplay_desktop_feature_div"><span class="a-price priceToPay"><span class="a-offscreen">₹2,399.00</span></span></div><div id="deliveryBlock"></div>
<div id="imgTagWrapperId"><img alt="Shirt" src="https://m.media-amazon.com/images/I/SHIRT_MAIN._SX342_.jpg" data-old-hires="https://m.media-amazon.com/images/I/SHIRT_MAIN._SL1500_.jpg" id="landingImage"
 data-a-dynamic-image="{&quot;https://m.media-amazon.com/images/I/SHIRT_MAIN._SX679_.jpg&quot;:[679,679]}"></div>
<script>'colorImages': { 'initial': [{"hiRes":"https://m.media-amazon.com/images/I/SHIRT_MAIN._SL1500_.jpg","large":"https://m.media-amazon.com/images/I/SHIRT_MAIN.jpg"},{"hiRes":"https://m.media-amazon.com/images/I/SHIRT_BACK._SL1500_.jpg"}]}</script>
<div id="similar"><img data-a-dynamic-image="{&quot;https://m.media-amazon.com/images/I/TSHIRT_SIMILAR._AC_.jpg&quot;:[200,200]}"></div>
</body></html>"""


def test_amazon_uses_only_the_viewed_products_images() -> None:
    import asyncio

    from app.scraper import BrightDataScraper

    class StubScraper(BrightDataScraper):
        async def _fetch_page(self, url: str) -> str:
            raise AssertionError("complete HTML data should skip the markdown fetch")

        async def _fetch_html_via_unlocker(self, url: str) -> str | None:
            return AMAZON_SHIRT_PAGE

    product = asyncio.run(StubScraper(SETTINGS).scrape("https://www.amazon.in/dp/B0SHIRT", "IN")).data
    assert product.image_urls[0] == "https://m.media-amazon.com/images/I/SHIRT_MAIN._SL1500_.jpg"
    assert all("TSHIRT" not in url for url in product.image_urls)
    assert "https://m.media-amazon.com/images/I/SHIRT_BACK._SL1500_.jpg" in product.image_urls
    assert product.outfit_slot == "top"


def test_bot_wall_page_is_not_parsed_as_the_product() -> None:
    import asyncio

    from app.scraper import BrightDataScraper

    class StubScraper(BrightDataScraper):
        async def _fetch_page(self, url: str) -> str:
            raise AssertionError("the direct HTML fetch has the product")

        async def _fetch_html_via_unlocker(self, url: str) -> str | None:
            return "<html><title>Amazon.in</title><p>Enter the characters you see below</p><img src='https://images-na.ssl-images-amazon.com/captcha/x.jpg'></html>"

        async def _fetch_html_directly(self, url: str) -> str | None:
            return AMAZON_SHIRT_PAGE

    product = asyncio.run(StubScraper(SETTINGS).scrape("https://www.amazon.in/dp/B0SHIRT", "IN")).data
    assert product.title == "Calvin Klein Jeans Men Shirt" and product.price.amount == 2399


def test_scrape_results_are_cached_and_shared() -> None:
    import asyncio

    from app.scraper import BrightDataScraper

    calls: list[str] = []

    class StubScraper(BrightDataScraper):
        async def _fetch_html_via_unlocker(self, url: str) -> str | None:
            calls.append(url)
            await asyncio.sleep(0.05)
            return AMAZON_SHIRT_PAGE

    async def run() -> list:
        scraper = StubScraper(SETTINGS)
        first = await asyncio.gather(*(scraper.scrape("https://www.amazon.in/dp/B0SHIRT", "IN") for _ in range(3)))
        again = await scraper.scrape("https://www.amazon.in/dp/B0SHIRT", "IN")
        again.data.title = "changed by a caller"
        return [*first, again, await scraper.scrape("https://www.amazon.in/dp/B0SHIRT", "IN")]

    results = asyncio.run(run())
    assert calls == ["https://www.amazon.in/dp/B0SHIRT"]
    assert results[-1].data.title == "Calvin Klein Jeans Men Shirt"


def test_standard_pose_prompt_reposes_and_keeps_identity() -> None:
    from app.tryon import OutfitPiece, tryon_prompt

    image = (b"", "image/png", "png")
    pieces = [OutfitPiece(image, "top", "CK BLACK Calvin Klein Jeans Men Shirt"), OutfitPiece(image, "footwear", "White sneakers")]
    standard = tryon_prompt(pieces)
    assert "ignore the pose in image 1" in standard
    assert "arms relaxed and straight down at the sides" in standard
    assert "from the top of the head to the soles of the shoes" in standard
    assert "same person as image 1" in standard and "glasses" in standard
    assert "image 2 is the top (CK BLACK Calvin Klein Jeans Men Shirt); image 3 is the footwear (White sneakers)" in standard
    keep = tryon_prompt(pieces, "keep")
    assert "Keep the person's own pose" in keep and "ignore the pose" not in keep


def test_tryon_passes_pose_and_rejects_unknown_pose() -> None:
    from app.models import GalleryItem

    session_settings = Settings(brightdata_api_token="test", anonymous_token_secret="a-secure-test-secret-that-is-long-enough")
    seen: list[str] = []

    class StubTryOn:
        def ensure_configured(self) -> None:
            pass

        async def fetch_image(self, url: str):
            return _tiny_png(), "image/png", "png"

        async def generate(self, person, product, category, product_name=None, pose="standard"):
            seen.append(pose)
            return _tiny_png(), "image/png", "png"

        async def save(self, user_id, person, product, result, category, product_source, product_url, items=None) -> GalleryItem:
            return GalleryItem(
                id="1", anonymous_user_id=user_id, category=category, product_source=product_source, product_url=product_url,
                person_image_url="https://example.com/p.png", product_image_url="https://example.com/i.png",
                result_image_url="https://example.com/r.png", model="test", created_at=datetime(2026, 9, 23, tzinfo=UTC),
            )

    app.dependency_overrides[get_runtime_settings] = lambda: session_settings
    app.dependency_overrides[get_tryon_service] = lambda: StubTryOn()
    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {client.post('/v1/sessions/anonymous').json()['access_token']}"}
            files = {"person_image": ("p.png", _tiny_png(), "image/png")}
            base = {"product_image_url": "https://assets.myntassets.com/shirt.jpg"}
            default = client.post("/v1/try-ons", headers=headers, files=files, data=base)
            keep = client.post("/v1/try-ons", headers=headers, files=files, data={**base, "pose": "keep"})
            bad = client.post("/v1/try-ons", headers=headers, files=files, data={**base, "pose": "dance"})
        assert default.status_code == 200 and keep.status_code == 200
        assert seen == ["standard", "keep"]
        assert bad.status_code == 422
    finally:
        app.dependency_overrides.clear()


AUTH_SETTINGS = Settings(
    openai_api_key="test",
    brightdata_api_token="test",
    anonymous_token_secret="a-secure-test-secret-that-is-long-enough",
    supabase_url="https://project.supabase.co",
    supabase_service_role_key="service-key",
    UNLIMITED_EMAILS=" Parthpatil2233@gmail.com , gajerajeet88@gmail.com",
)
AUTH_USER_ID = "8d0f6a52-4b8e-4f0e-9d7a-1f2b3c4d5e6f"


def _mock_supabase_auth(monkeypatch, handler) -> None:
    import httpx as _httpx

    real_client = _httpx.AsyncClient
    monkeypatch.setattr("app.email_auth.httpx.AsyncClient", lambda **kwargs: real_client(transport=_httpx.MockTransport(handler), **kwargs))


def test_email_code_sign_in_gives_listed_emails_unlimited_looks(monkeypatch) -> None:
    import json as _json
    import httpx as _httpx

    seen: list[tuple[str, dict]] = []

    def handler(request: _httpx.Request) -> _httpx.Response:
        seen.append((request.url.path, _json.loads(request.content or b"{}")))
        assert request.headers["apikey"] == "service-key"
        if request.url.path == "/auth/v1/otp":
            return _httpx.Response(200, json={})
        if request.url.path == "/auth/v1/verify":
            if request.content and _json.loads(request.content)["token"] != "123456":
                return _httpx.Response(403, json={"msg": "Token has expired or is invalid"})
            email = _json.loads(request.content)["email"]
            return _httpx.Response(200, json={"access_token": "x", "user": {"id": AUTH_USER_ID, "email": email}})
        return _httpx.Response(404)

    _mock_supabase_auth(monkeypatch, handler)
    app.dependency_overrides[get_runtime_settings] = lambda: AUTH_SETTINGS
    try:
        with TestClient(app) as client:
            assert client.post("/v1/auth/email/code", json={"email": " ParthPatil2233@Gmail.com "}).status_code == 204
            wrong = client.post("/v1/auth/email/verify", json={"email": "parthpatil2233@gmail.com", "code": "000000"})
            signed_in = client.post("/v1/auth/email/verify", json={"email": "parthpatil2233@gmail.com", "code": "123 456"})
            me = client.get("/v1/me", headers={"Authorization": f"Bearer {signed_in.json()['access_token']}"})
            gallery = client.get("/v1/gallery", headers={"Authorization": f"Bearer {signed_in.json()['access_token']}"})
            other = client.post("/v1/auth/email/verify", json={"email": "someone@example.com", "code": "123456"})
            anonymous = client.post("/v1/sessions/anonymous").json()
            anonymous_me = client.get("/v1/me", headers={"Authorization": f"Bearer {anonymous['access_token']}"})
            bad_email = client.post("/v1/auth/email/code", json={"email": "not-an-email"})
    finally:
        app.dependency_overrides.clear()

    assert seen[0] == ("/auth/v1/otp", {"email": "parthpatil2233@gmail.com", "create_user": True})
    assert wrong.status_code == 401
    assert signed_in.status_code == 200
    assert signed_in.json()["unlimited"] is True and signed_in.json()["anonymous_user_id"] == AUTH_USER_ID
    assert me.json() == {"user_id": AUTH_USER_ID, "email": "parthpatil2233@gmail.com", "unlimited": True}
    assert gallery.status_code == 503  # authenticated; storage is not configured in tests
    assert other.json()["unlimited"] is False
    assert anonymous_me.json()["unlimited"] is False and anonymous_me.json()["email"] is None
    assert bad_email.status_code == 422


def test_unlimited_is_rechecked_so_removing_an_email_revokes_it(monkeypatch) -> None:
    import httpx as _httpx

    _mock_supabase_auth(monkeypatch, lambda request: _httpx.Response(200, json={"id": AUTH_USER_ID, "email": "gajerajeet88@gmail.com"}))
    app.dependency_overrides[get_runtime_settings] = lambda: AUTH_SETTINGS
    try:
        with TestClient(app) as client:
            session = client.post("/v1/auth/email/link", json={"access_token": "a" * 40}).json()
            app.dependency_overrides[get_runtime_settings] = lambda: AUTH_SETTINGS.model_copy(update={"unlimited_emails_csv": ""})
            me = client.get("/v1/me", headers={"Authorization": f"Bearer {session['access_token']}"}).json()
    finally:
        app.dependency_overrides.clear()
    assert session["unlimited"] is True
    assert me["unlimited"] is False


import httpx as _httpx_module
from pydantic import SecretStr

_REAL_ASYNC_CLIENT = _httpx_module.AsyncClient
_FAKE_HANDLERS: dict[str, object] = {}


def _install_fakes(monkeypatch, **handlers) -> None:
    """Route api.razorpay.com to the Razorpay fake and everything else to the Supabase fake."""
    _FAKE_HANDLERS.update(handlers)

    def route(request):
        key = "razorpay" if request.url.host == "api.razorpay.com" else "supabase"
        return _FAKE_HANDLERS[key](request)

    monkeypatch.setattr(
        "app.tryon.httpx.AsyncClient",
        lambda **kwargs: _REAL_ASYNC_CLIENT(transport=_httpx_module.MockTransport(route), **{k: v for k, v in kwargs.items() if k != "transport"}),
    )


LIMIT_SETTINGS = Settings(
    openai_api_key="test",
    brightdata_api_token="test",
    gemini_api_key="test",
    anonymous_token_secret="a-secure-test-secret-that-is-long-enough",
    supabase_url="https://project.supabase.co",
    supabase_service_role_key="service-key",
    razorpay_key_id="rzp_test_key",
    razorpay_key_secret="rzp-secret",
    razorpay_webhook_secret="hook-secret",
    UNLIMITED_EMAILS="vip@example.com",
)


class FakeSupabase:
    """Records PostgREST calls; consume_look answers from `grant`, look_grants GET from `rows`."""

    def __init__(self, grant=None, missing=False, rows=None):
        import json as _json
        import httpx as _httpx

        self.calls: list[tuple[str, str, object]] = []
        self.grant, self.missing, self.rows = grant, missing, rows or []

        def handler(request: _httpx.Request) -> _httpx.Response:
            body = _json.loads(request.content) if request.content else None
            path = request.url.path.replace("/rest/v1/", "")
            self.calls.append((request.method, path, body if body is not None else dict(request.url.params)))
            if self.missing:
                return _httpx.Response(404, json={"code": "PGRST202", "message": "Could not find the function"})
            if path == "rpc/consume_look":
                return _httpx.Response(200, content=_json.dumps(self.grant).encode(), headers={"content-type": "application/json"})
            if path in ("rpc/refund_look", "rpc/ensure_free_looks"):
                return _httpx.Response(204)
            if path == "look_grants" and request.method == "GET":
                return _httpx.Response(200, json=self.rows)
            if path == "look_grants" and request.method == "POST":
                return _httpx.Response(201)
            return _httpx.Response(404)

        self.handler = handler

    def install(self, monkeypatch) -> None:
        _install_fakes(monkeypatch, supabase=self.handler)

    def paths(self) -> list[str]:
        return [path for _, path, _ in self.calls]


def _email_token(email: str, settings: Settings = LIMIT_SETTINGS) -> str:
    from app.anonymous_auth import create_email_session

    return create_email_session(AUTH_USER_ID, email, settings).access_token


def _tryon_with_bad_photo(client: TestClient, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {"Authorization": f"Bearer {client.post('/v1/sessions/anonymous').json()['access_token']}"}
    return client.post(
        "/v1/try-ons",
        headers=headers,
        files={"person_image": ("person.txt", b"not an image", "text/plain"), "product_image": ("product.png", _tiny_png(), "image/png")},
        data={"category": "shirt"},
    )


def _limit_client(fake: FakeSupabase, monkeypatch):
    fake.install(monkeypatch)
    app.dependency_overrides[get_runtime_settings] = lambda: LIMIT_SETTINGS
    service = TryOnService(LIMIT_SETTINGS)
    app.dependency_overrides[get_tryon_service] = lambda: service
    return TestClient(app)


def test_guests_must_sign_in_before_a_try_on(monkeypatch) -> None:
    fake = FakeSupabase(grant="g-1")
    try:
        with _limit_client(fake, monkeypatch) as client:
            response = _tryon_with_bad_photo(client, None)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "sign_in_required"
    assert "rpc/consume_look" not in fake.paths()


def test_no_looks_left_stops_before_generating(monkeypatch) -> None:
    fake = FakeSupabase(grant=None)
    try:
        with _limit_client(fake, monkeypatch) as client:
            response = _tryon_with_bad_photo(client, _email_token("buyer@example.com"))
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 402
    assert response.json()["detail"]["code"] == "no_looks_left"
    assert fake.calls[0] == ("POST", "rpc/consume_look", {"p_user": AUTH_USER_ID, "p_free_looks": 3})


def test_failed_try_on_gives_the_look_back(monkeypatch) -> None:
    fake = FakeSupabase(grant="grant-7")
    try:
        with _limit_client(fake, monkeypatch) as client:
            response = _tryon_with_bad_photo(client, _email_token("buyer@example.com"))
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 400  # the photo was rejected after the look was reserved
    assert fake.paths() == ["rpc/consume_look", "rpc/refund_look"]
    assert fake.calls[1][2] == {"p_grant": "grant-7"}


def test_unlimited_accounts_and_missing_schema_skip_the_ledger(monkeypatch) -> None:
    fake = FakeSupabase(grant=None)
    missing = FakeSupabase(missing=True)
    try:
        with _limit_client(fake, monkeypatch) as client:
            vip = _tryon_with_bad_photo(client, _email_token("VIP@example.com"))
        with _limit_client(missing, monkeypatch) as client:
            not_set_up = _tryon_with_bad_photo(client, _email_token("buyer@example.com"))
    finally:
        app.dependency_overrides.clear()
    assert vip.status_code == 400 and fake.calls == []
    assert not_set_up.status_code == 400 and missing.paths() == ["rpc/consume_look"]


def test_balance_adds_up_active_grants(monkeypatch) -> None:
    fake = FakeSupabase(rows=[
        {"kind": "free", "looks": 3, "used": 3, "expires_at": "2026-10-01T00:00:00+00:00"},
        {"kind": "pass", "looks": 10, "used": 2, "expires_at": "2026-10-02T00:00:00+00:00"},
        {"kind": "plus", "looks": 25, "used": 0, "expires_at": "2026-10-24T00:00:00+00:00"},
    ])
    try:
        with _limit_client(fake, monkeypatch) as client:
            balance = client.get("/v1/looks/balance", headers={"Authorization": f"Bearer {_email_token('buyer@example.com')}"}).json()
            guest_token = client.post("/v1/sessions/anonymous").json()["access_token"]
            guest = client.get("/v1/looks/balance", headers={"Authorization": f"Bearer {guest_token}"}).json()
    finally:
        app.dependency_overrides.clear()
    assert balance["remaining"] == 33 and balance["plan"] == "plus" and balance["signed_in"] is True
    assert [g["remaining"] for g in balance["grants"]] == [0, 8, 25]
    assert fake.paths()[0] == "rpc/ensure_free_looks"
    assert guest == {"signed_in": False, "unlimited": False, "enforced": True, "remaining": None, "plan": "free", "free_looks_per_month": 3, "grants": []}


class FakeRazorpay:
    def __init__(self, objects: dict | None = None):
        import base64
        import json as _json
        import httpx as _httpx

        self.requests: list[tuple[str, str, object]] = []
        self.objects = objects or {}
        self.plans: list[dict] = []

        def handler(request: _httpx.Request) -> _httpx.Response:
            body = _json.loads(request.content) if request.content else None
            path = request.url.path.replace("/v1", "", 1)
            self.requests.append((request.method, path, body))
            assert request.headers["authorization"] == "Basic " + base64.b64encode(b"rzp_test_key:rzp-secret").decode()
            if path == "/orders" and request.method == "POST":
                return _httpx.Response(200, json={"id": "order_New1", "amount": body["amount"], "notes": body["notes"]})
            if path == "/plans" and request.method == "GET":
                return _httpx.Response(200, json={"items": self.plans})
            if path == "/plans" and request.method == "POST":
                plan = {"id": f"plan_{len(self.plans) + 1}", "notes": body["notes"]}
                self.plans.append(plan)
                return _httpx.Response(200, json=plan)
            if path == "/subscriptions" and request.method == "POST":
                return _httpx.Response(200, json={"id": "sub_New1", "status": "created"})
            if path.endswith("/capture"):
                return _httpx.Response(200, json={**self.objects[path.removesuffix("/capture")], "status": "captured"})
            if path in self.objects:
                return _httpx.Response(200, json=self.objects[path])
            return _httpx.Response(400, json={"error": {"description": "The id provided does not exist"}})

        self.handler = handler

    def install(self, monkeypatch) -> None:
        _install_fakes(monkeypatch, razorpay=self.handler)


def _rzp_signature(message: str, secret: str = "rzp-secret") -> str:
    import hashlib, hmac

    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def test_checkout_creates_orders_and_subscriptions(monkeypatch) -> None:
    from app.billing import Billing

    Billing._plan_ids.clear()
    razorpay = FakeRazorpay()
    razorpay.install(monkeypatch)
    try:
        with _limit_client(FakeSupabase(), monkeypatch) as client:
            headers = {"Authorization": f"Bearer {_email_token('buyer@example.com')}"}
            pass_ = client.post("/v1/billing/checkout", json={"plan": "pass"}, headers=headers).json()
            plus = client.post("/v1/billing/checkout", json={"plan": "plus", "billing": "yearly"}, headers=headers).json()
            again = client.post("/v1/billing/checkout", json={"plan": "plus", "billing": "yearly"}, headers=headers).json()
            guest_token = client.post("/v1/sessions/anonymous").json()["access_token"]
            guest = client.post("/v1/billing/checkout", json={"plan": "pro"}, headers={"Authorization": f"Bearer {guest_token}"})
            config = client.get("/v1/billing/config").json()
    finally:
        app.dependency_overrides.clear()
    assert pass_["order_id"] == "order_New1" and pass_["amount"] == 12900 and pass_["key_id"] == "rzp_test_key" and pass_["subscription_id"] is None
    order = razorpay.requests[0][2]
    assert order["currency"] == "INR" and order["notes"] == {"user_id": AUTH_USER_ID, "plan": "pass", "billing": "once"}
    assert plus["subscription_id"] == "sub_New1" and plus["amount"] == 329900 and plus["email"] == "buyer@example.com"
    created_plans = [body for method, path, body in razorpay.requests if (method, path) == ("POST", "/plans")]
    assert len(created_plans) == 1  # the Razorpay plan is created once, then reused
    assert created_plans[0]["period"] == "yearly" and created_plans[0]["item"]["amount"] == 329900
    subscriptions = [body for method, path, body in razorpay.requests if (method, path) == ("POST", "/subscriptions")]
    assert subscriptions[0]["plan_id"] == "plan_1" and subscriptions[0]["total_count"] == 10 and subscriptions[0]["notes"]["plan"] == "plus"
    assert again["subscription_id"] == "sub_New1"
    assert guest.status_code == 403
    assert config == {"enabled": True, "test_mode": True, "provider": "razorpay"}


def test_live_razorpay_keys_are_refused_unless_allowed() -> None:
    from app.billing import Billing

    live = LIMIT_SETTINGS.model_copy(update={"razorpay_key_id": "rzp_live_abc"})
    assert Billing(live, None).enabled is False and Billing(live, None).test_mode is False
    assert Billing(live.model_copy(update={"razorpay_allow_live": True}), None).enabled is True


def test_pass_payment_is_verified_captured_and_granted_once(monkeypatch) -> None:
    razorpay = FakeRazorpay({
        "/orders/order_Mine": {"id": "order_Mine", "amount": 12900, "created_at": 1_790_000_000, "notes": {"user_id": AUTH_USER_ID, "plan": "pass"}},
        "/payments/pay_Mine": {"id": "pay_Mine", "order_id": "order_Mine", "amount": 12900, "currency": "INR", "status": "authorized"},
        "/orders/order_Other": {"id": "order_Other", "amount": 12900, "notes": {"user_id": "00000000-0000-4000-8000-000000000000", "plan": "pass"}},
    })
    razorpay.install(monkeypatch)
    fake = FakeSupabase(rows=[{"kind": "pass", "looks": 10, "used": 0, "expires_at": "2026-09-28T00:00:00+00:00"}])
    try:
        with _limit_client(fake, monkeypatch) as client:
            headers = {"Authorization": f"Bearer {_email_token('buyer@example.com')}"}
            good = {"razorpay_payment_id": "pay_Mine", "razorpay_order_id": "order_Mine", "razorpay_signature": _rzp_signature("order_Mine|pay_Mine")}
            forged = client.post("/v1/billing/confirm", json={**good, "razorpay_signature": _rzp_signature("order_Mine|pay_Mine", "wrong")}, headers=headers)
            ok = client.post("/v1/billing/confirm", json=good, headers=headers)
            other = client.post("/v1/billing/confirm", json={"razorpay_payment_id": "pay_Mine", "razorpay_order_id": "order_Other",
                                                             "razorpay_signature": _rzp_signature("order_Other|pay_Mine")}, headers=headers)
    finally:
        app.dependency_overrides.clear()
    assert forged.status_code == 400
    assert ok.status_code == 200 and ok.json()["plan"] == "pass" and ok.json()["remaining"] == 10
    assert ("POST", "/payments/pay_Mine/capture", {"amount": 12900, "currency": "INR"}) in razorpay.requests
    grant = next(call for call in fake.calls if call[:2] == ("POST", "look_grants"))
    assert grant[2][0]["payment_ref"] == "order_Mine" and grant[2][0]["looks"] == 10 and grant[2][0]["expires_at"].startswith("2026-09-28")
    assert other.status_code == 403


def test_subscription_confirm_and_webhook_grant_plan_looks(monkeypatch) -> None:
    subscription = {"id": "sub_Mine", "status": "active", "current_start": 1_790_000_000, "current_end": 1_792_592_000,
                    "notes": {"user_id": AUTH_USER_ID, "plan": "plus", "billing": "monthly"}}
    razorpay = FakeRazorpay({"/subscriptions/sub_Mine": subscription,
                             "/subscriptions/sub_Waiting": {**subscription, "id": "sub_Waiting", "status": "authenticated", "current_start": None}})
    razorpay.install(monkeypatch)
    fake = FakeSupabase(rows=[{"kind": "plus", "looks": 25, "used": 0, "expires_at": "2026-10-24T00:00:00+00:00"}])
    yearly = {"event": "subscription.charged", "payload": {"subscription": {"entity": {
        **subscription, "id": "sub_Year", "current_end": 1_821_536_000, "notes": {"user_id": AUTH_USER_ID, "plan": "pro", "billing": "yearly"}}}}}
    order_paid = {"event": "order.paid", "payload": {"order": {"entity": {"id": "order_Hook", "created_at": 1_790_000_000, "notes": {"user_id": AUTH_USER_ID, "plan": "pass"}}}}}
    try:
        with _limit_client(fake, monkeypatch) as client:
            headers = {"Authorization": f"Bearer {_email_token('buyer@example.com')}"}
            ok = client.post("/v1/billing/confirm", headers=headers, json={
                "razorpay_payment_id": "pay_Sub", "razorpay_subscription_id": "sub_Mine", "razorpay_signature": _rzp_signature("pay_Sub|sub_Mine")})
            waiting = client.post("/v1/billing/confirm", headers=headers, json={
                "razorpay_payment_id": "pay_Sub", "razorpay_subscription_id": "sub_Waiting", "razorpay_signature": _rzp_signature("pay_Sub|sub_Waiting")})
            body = json.dumps(yearly).encode()
            forged = client.post("/v1/billing/webhook", content=body, headers={"X-Razorpay-Signature": _rzp_signature(body.decode(), "not-the-secret")})
            year = client.post("/v1/billing/webhook", content=body, headers={"X-Razorpay-Signature": _rzp_signature(body.decode(), "hook-secret")})
            body = json.dumps(order_paid).encode()
            paid = client.post("/v1/billing/webhook", content=body, headers={"X-Razorpay-Signature": _rzp_signature(body.decode(), "hook-secret")})
    finally:
        app.dependency_overrides.clear()
    assert ok.status_code == 200 and ok.json()["plan"] == "plus"
    assert waiting.status_code == 409
    assert forged.status_code == 400 and year.status_code == 200 and paid.status_code == 200
    inserts = [call[2] for call in fake.calls if call[:2] == ("POST", "look_grants")]
    assert inserts[0][0]["payment_ref"] == "sub_Mine:1790000000" and inserts[0][0]["looks"] == 25
    months = inserts[1]
    assert len(months) == 12 and {row["looks"] for row in months} == {60} and months[1]["starts_at"] == months[0]["expires_at"]
    assert inserts[2][0]["payment_ref"] == "order_Hook" and inserts[2][0]["kind"] == "pass"


def test_invalid_try_on_form_never_keeps_a_look(monkeypatch) -> None:
    fake = FakeSupabase(grant="grant-9")
    try:
        with _limit_client(fake, monkeypatch) as client:
            response = client.post("/v1/try-ons", headers={"Authorization": f"Bearer {_email_token('buyer@example.com')}"}, data={"category": "shirt"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 422
    assert fake.paths() in ([], ["rpc/consume_look", "rpc/refund_look"])
