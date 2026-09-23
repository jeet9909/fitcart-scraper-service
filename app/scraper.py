import asyncio
import html
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from app.config import Settings
from app.security import validate_public_url
from app.models import Money, ProductData, ScrapeResponse

logger = logging.getLogger(__name__)

SHARE_HOST_DESTINATIONS = {
    "amzn.in": ("amazon.in",),
    "fkrt.it": ("flipkart.com",),
}


class ScrapeProviderError(RuntimeError):
    def __init__(self, message: str, code: str = "provider_failed") -> None:
        super().__init__(message)
        self.code = code


def _amount(value: str) -> float | None:
    try:
        return float(value.replace(",", "").strip())
    except ValueError:
        return None


def _strip_security_wrapper(text: str) -> str:
    match = re.search(
        r"=====UNTRUSTED_([A-Za-z0-9]+)_BEGIN=====\s*(.*?)\s*=====UNTRUSTED_\1_END=====",
        text,
        flags=re.DOTALL,
    )
    return match.group(2).strip() if match else text.strip()


def _is_allowed_destination(host: str, allowed_roots: tuple[str, ...]) -> bool:
    return any(host == root or host.endswith(f".{root}") for root in allowed_roots)


def _resolve_share_url(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    allowed_roots = SHARE_HOST_DESTINATIONS.get(host)
    if not allowed_roots:
        return url

    headers = {"User-Agent": "Mozilla/5.0 (compatible; FitCartScraper/1.0)"}
    last_error: Exception | None = None
    for method in ("HEAD", "GET"):
        try:
            request = Request(url, headers=headers, method=method)
            with urlopen(request, timeout=12) as response:
                resolved = response.geturl()
            resolved_host = (urlsplit(resolved).hostname or "").lower()
            if not _is_allowed_destination(resolved_host, allowed_roots):
                raise ScrapeProviderError("Product share link redirected to an unexpected domain")
            return resolved
        except ScrapeProviderError:
            raise
        except Exception as exc:
            last_error = exc
    logger.warning("Could not resolve product share URL host=%s error=%s", host, last_error)
    raise ScrapeProviderError(
        "The product share URL is invalid, expired, or unavailable",
        code="invalid_share_link",
    )


PRODUCT_IMAGE_HOSTS = (
    "m.media-amazon.com/images/i/",
    "images-na.ssl-images-amazon.com/images/i/",
    "assets.myntassets.com",
    "flixcart.com/image",
    "assets.ajio.com",
    "images.meesho.com",
    "static.nike.com",
)
JUNK_IMAGE_MARKERS = (
    "sprite", "icon", "logo", "pixel", "badge", "banner", "placeholder", "loading",
    "transparent", "/nav-", "prime_", "star", "rating", "avatar", "flag", "1x1",
)
IMAGE_EXTENSION = r"\.(?:jpe?g|png|webp)"
# Myntra serves templated image URLs such as
# http://assets.myntassets.com/h_($height),q_($qualityPercentage),w_($width)/v1/assets/...
MYNTRA_TEMPLATE = re.compile(r"h_\(\$height\),q_\(\$qualityPercentage\),w_\(\$width\)")


def _normalize_image_url(raw: str, base_url: str | None = None) -> str | None:
    candidate = html.unescape(raw.strip().strip("'\"")).replace("\\/", "/").replace("\\u002F", "/")
    candidate = MYNTRA_TEMPLATE.sub("h_1440,q_90,w_1080", candidate)
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    elif base_url and candidate.startswith("/"):
        candidate = urljoin(base_url, candidate)
    if not re.match(r"^https?://", candidate, re.IGNORECASE):
        return None
    if candidate.lower().startswith("http://"):
        candidate = f"https://{candidate[7:]}"
    lowered = candidate.lower()
    if lowered.endswith((".gif", ".svg")) or ".gif?" in lowered or ".svg?" in lowered:
        return None
    if any(host in lowered for host in PRODUCT_IMAGE_HOSTS):
        return candidate
    path = urlsplit(lowered).path
    if any(marker in path for marker in JUNK_IMAGE_MARKERS):
        return None
    return candidate


def _rank_images(urls: list[str]) -> list[str]:
    unique = list(dict.fromkeys(urls))
    known = [url for url in unique if any(host in url.lower() for host in PRODUCT_IMAGE_HOSTS)]
    return (known + [url for url in unique if url not in known])[:30]


def _extract_image_urls(text: str, base_url: str | None = None) -> list[str]:
    raw: list[str] = []
    # Markdown images, including titles: ![alt](url "title") and Myntra templates with parentheses.
    raw += re.findall(r"!\[[^\]]*\]\(\s*<?((?:https?:)?//(?:[^\s()<>]|\([^\s()]*\))+)", text)
    # Bare image URLs anywhere in the text (links, attributes, JSON).
    raw += re.findall(
        rf"((?:https?:)?//[^\s\"'<>()\[\]]+?{IMAGE_EXTENSION}(?:\?[^\s\"'<>()\[\]]*)?)",
        MYNTRA_TEMPLATE.sub("h_1440,q_90,w_1080", text),
        flags=re.IGNORECASE,
    )
    images = []
    for item in raw:
        normalized = _normalize_image_url(item, base_url)
        if normalized:
            images.append(normalized)
    return _rank_images(images)


def _json_ld_images(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        image = value.get("image")
        if isinstance(image, str):
            found.append(image)
        elif isinstance(image, list):
            found += [item if isinstance(item, str) else item.get("url", "") for item in image if isinstance(item, (str, dict))]
        elif isinstance(image, dict) and isinstance(image.get("url"), str):
            found.append(image["url"])
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                found += _json_ld_images(nested)
    elif isinstance(value, list):
        for item in value:
            found += _json_ld_images(item)
    return found


def _images_from_html(page: str, base_url: str) -> list[str]:
    raw: list[str] = []
    for prop in ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"):
        pattern = re.escape(prop)
        raw += re.findall(rf"<meta[^>]+(?:property|name)=[\"']{pattern}[\"'][^>]*content=[\"']([^\"']+)", page, re.IGNORECASE)
        raw += re.findall(rf"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name)=[\"']{pattern}[\"']", page, re.IGNORECASE)
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page, re.IGNORECASE | re.DOTALL):
        try:
            raw += _json_ld_images(json.loads(block))
        except ValueError:
            continue
    # Amazon main image attributes and gallery JSON.
    raw += re.findall(r"data-old-hires=[\"']([^\"']+)", page)
    raw += re.findall(r"\"hiRes\"\s*:\s*\"([^\"]+)\"", page)
    for dynamic in re.findall(r"data-a-dynamic-image=[\"']([^\"']+)", page):
        try:
            raw += list(json.loads(html.unescape(dynamic)).keys())
        except ValueError:
            continue
    images = [image for item in raw if (image := _normalize_image_url(item, base_url))]
    return _rank_images(images + _extract_image_urls(page, base_url))


def _parse_product(markdown: str, url: str) -> ProductData:
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    headings = [re.sub(r"^#+\s*", "", line).strip() for line in lines if re.match(r"^#{1,3}\s+", line)]
    title = headings[0] if headings else (lines[0][:300] if lines else urlsplit(url).hostname or "Product")

    price_matches = re.findall(r"(?:₹|INR\s*)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", markdown, flags=re.IGNORECASE)
    prices = [amount for raw in price_matches if (amount := _amount(raw)) is not None]
    price = prices[0] if prices else None
    original = next((candidate for candidate in prices[1:] if price is not None and candidate > price), None)
    discount = round((original - price) / original * 100, 2) if price is not None and original else None

    images = _extract_image_urls(markdown, url)

    rating_match = re.search(r"\b([0-4](?:\.\d+)?|5(?:\.0+)?)\s*(?:out of 5|/\s*5|stars?|★)", markdown, re.IGNORECASE)
    rating = float(rating_match.group(1)) if rating_match else None
    reviews_match = re.search(r"([0-9][0-9,]*)\s+(?:ratings?|reviews?)", markdown, re.IGNORECASE)
    review_count = int(reviews_match.group(1).replace(",", "")) if reviews_match else None

    lowered = markdown.lower()
    availability = "out_of_stock" if any(term in lowered for term in ("out of stock", "currently unavailable", "sold out")) else "in_stock" if any(term in lowered for term in ("in stock", "add to cart", "buy now")) else "unknown"
    description_lines = [line for line in lines if not line.startswith(("#", "![", "["))]
    description = " ".join(description_lines[:8])[:2000] or None

    return ProductData(
        source_url=url,
        store=(urlsplit(url).hostname or "").removeprefix("www."),
        title=title[:500],
        description=description,
        price=Money(amount=price, currency="INR"),
        original_price=Money(amount=original, currency="INR") if original is not None else None,
        discount_percent=discount,
        availability=availability,
        rating=rating,
        review_count=review_count,
        image_urls=images,
    )


class BrightDataScraper:
    def __init__(self, settings: Settings, client: object | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)

    async def _call_brightdata(self, tool: str, url: str, optional: bool = False) -> str | None:
        token = self.settings.brightdata_api_token.get_secret_value()
        server_url = f"https://mcp.brightdata.com/mcp?token={token}"
        async with streamablehttp_client(server_url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                if optional:
                    tools = await session.list_tools()
                    if tool not in {item.name for item in tools.tools}:
                        return None
                result = await session.call_tool(tool, {"url": url})
        text = "\n".join(block.text for block in result.content if isinstance(block, TextContent))
        if result.isError or not text.strip():
            if optional:
                return None
            raise ScrapeProviderError(text.strip() or "Bright Data returned no page content")
        return text

    async def _fetch_page(self, url: str) -> str:
        return await self._call_brightdata("scrape_as_markdown", url) or ""

    async def _fetch_html_directly(self, url: str) -> str | None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
            for _ in range(5):
                await asyncio.to_thread(validate_public_url, url)
                response = await client.get(url, headers=headers)
                if response.is_redirect and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                return response.text if response.status_code < 400 else None
        return None

    async def _fallback_images(self, url: str) -> list[str]:
        """Find product images in the page HTML when the markdown has none."""
        for source, fetch in (
            ("direct", lambda: self._fetch_html_directly(url)),
            ("brightdata_html", lambda: self._call_brightdata("scrape_as_html", url, optional=True)),
        ):
            try:
                page = await asyncio.wait_for(fetch(), timeout=self.settings.scrape_timeout_seconds)
            except Exception as exc:
                logger.info("Image fallback %s failed host=%s type=%s", source, urlsplit(url).hostname, type(exc).__name__)
                continue
            images = _images_from_html(_strip_security_wrapper(page), url) if page else []
            if images:
                logger.info("Image fallback %s found %d images host=%s", source, len(images), urlsplit(url).hostname)
                return images
        return []

    async def scrape(self, url: str, country: str) -> ScrapeResponse:
        del country
        try:
            async with self._semaphore:
                resolved_url = await asyncio.to_thread(_resolve_share_url, url)
                content = await asyncio.wait_for(self._fetch_page(resolved_url), timeout=self.settings.scrape_timeout_seconds)
                content = _strip_security_wrapper(content)
                if "page not found" in content.lower() and len(content) < 500:
                    raise ScrapeProviderError("The product page was not found")
                product = _parse_product(content, url)
                if not product.image_urls:
                    product.image_urls = await self._fallback_images(resolved_url)
                if not product.image_urls:
                    logger.warning(
                        "No product images found host=%s title=%r markdown_start=%r",
                        urlsplit(resolved_url).hostname,
                        product.title[:120],
                        content[:1500],
                    )
        except TimeoutError as exc:
            raise ScrapeProviderError("Product scraping timed out") from exc
        except ScrapeProviderError:
            raise
        except Exception as exc:
            safe_message = str(exc).replace(self.settings.brightdata_api_token.get_secret_value(), "[REDACTED]")
            logger.error("Product scraping failed host=%s type=%s error=%s", urlsplit(url).hostname, type(exc).__name__, safe_message[:2000])
            raise ScrapeProviderError(f"Product scraping failed: {safe_message[:500]}") from exc
        return ScrapeResponse(data=product, scraped_at=self.clock())
