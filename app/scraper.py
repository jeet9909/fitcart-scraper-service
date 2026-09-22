import asyncio
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from app.config import Settings
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
    raise ScrapeProviderError("The product share URL could not be resolved")


def _parse_product(markdown: str, url: str) -> ProductData:
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    headings = [re.sub(r"^#+\s*", "", line).strip() for line in lines if re.match(r"^#{1,3}\s+", line)]
    title = headings[0] if headings else (lines[0][:300] if lines else urlsplit(url).hostname or "Product")

    price_matches = re.findall(r"(?:₹|INR\s*)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", markdown, flags=re.IGNORECASE)
    prices = [amount for raw in price_matches if (amount := _amount(raw)) is not None]
    price = prices[0] if prices else None
    original = next((candidate for candidate in prices[1:] if price is not None and candidate > price), None)
    discount = round((original - price) / original * 100, 2) if price is not None and original else None

    images = []
    for image in re.findall(r"!\[[^\]]*\]\((https?://[^\s)]+)", markdown):
        if image not in images:
            images.append(image)

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
        image_urls=images[:30],
    )


class BrightDataScraper:
    def __init__(self, settings: Settings, client: object | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)

    async def _fetch_page(self, url: str) -> str:
        token = self.settings.brightdata_api_token.get_secret_value()
        server_url = f"https://mcp.brightdata.com/mcp?token={token}"
        async with streamablehttp_client(server_url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool("scrape_as_markdown", {"url": url})
        text = "\n".join(block.text for block in result.content if isinstance(block, TextContent))
        if result.isError or not text.strip():
            raise ScrapeProviderError(text.strip() or "Bright Data returned no page content")
        return text

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
        except TimeoutError as exc:
            raise ScrapeProviderError("Product scraping timed out") from exc
        except ScrapeProviderError:
            raise
        except Exception as exc:
            safe_message = str(exc).replace(self.settings.brightdata_api_token.get_secret_value(), "[REDACTED]")
            logger.error("Product scraping failed host=%s type=%s error=%s", urlsplit(url).hostname, type(exc).__name__, safe_message[:2000])
            raise ScrapeProviderError(f"Product scraping failed: {safe_message[:500]}") from exc
        return ScrapeResponse(data=product, scraped_at=self.clock())
