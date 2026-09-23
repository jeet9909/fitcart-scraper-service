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
    "transparent", "/nav-", "prime_", "star", "rating", "avatar", "flag", "1x1", "captcha",
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
    # Page JSON often escapes slashes: "https:\/\/assets.myntassets.com\/...".
    text = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
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


OUTFIT_SLOTS = ("top", "bottom", "dress", "outerwear", "footwear", "jewelry", "accessory", "other")
_SLOT_KEYWORDS = (
    ("footwear", ("shoe", "sneaker", "sandal", "slipper", "flip flop", "flip-flop", "loafer", "boot", "heel", "footwear", "mojari", "jutti", "clog")),
    ("jewelry", ("jewel", "necklace", "earring", "ring", "bracelet", "bangle", "pendant", "chain", "anklet", "nose pin", "mangalsutra", "kada")),
    ("dress", ("dress", "gown", "jumpsuit", "saree", "sari", "lehenga", "playsuit", "romper", "co-ord", "kurta set", "anarkali")),
    ("outerwear", ("jacket", "blazer", "coat", "hoodie", "sweatshirt", "cardigan", "shrug", "sweater", "pullover", "waistcoat", "nehru")),
    ("bottom", ("trouser", "jean", "pant", "short", "skirt", "legging", "jogger", "chino", "cargo", "palazzo", "track pant", "dhoti", "salwar", "churidar", "pyjama")),
    ("top", ("shirt", "t-shirt", "tshirt", "tee", "top", "kurta", "kurti", "polo", "blouse", "tunic", "vest", "tank", "camisole", "crop")),
    ("accessory", ("watch", "belt", "bag", "wallet", "cap", "hat", "sunglass", "scarf", "stole", "tie", "sock", "backpack", "clutch")),
)


def outfit_slot(*texts: str | None) -> str | None:
    """Map store category / title text to one outfit slot, checking the most specific text first."""
    for text in texts:
        lowered = f" {(text or '').lower()} "
        for slot, words in _SLOT_KEYWORDS:
            if any(re.search(rf"\b{re.escape(word)}", lowered) for word in words):
                return slot
    return None


def _clean_title(title: str) -> str:
    title = html.unescape(re.sub(r"\s+", " ", title)).strip()
    title = re.split(r"\s+\|\s+", title)[0]
    title = re.sub(r"^Buy\s+", "", title, flags=re.IGNORECASE)
    # "Allen Solly Men Trousers - Trousers for Men 42057155" and "... Online at Best Prices in India".
    title = re.sub(r"\s+-\s+[^-]*\b\d{6,}\b.*$", "", title)
    title = re.sub(r"\s*[-:]?\s*(?:Buy\s+)?Online at (?:Best|Low) Prices?.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^Amazon\.in\s*:\s*", "", title, flags=re.IGNORECASE)
    return title.strip(" -:|") or "Product"


def _json_blocks(page: str) -> list[object]:
    blocks: list[object] = []
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page, re.IGNORECASE | re.DOTALL):
        try:
            blocks.append(json.loads(html.unescape(block.strip())))
        except ValueError:
            continue
    return blocks


def _walk(value: object):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _is_type(node: dict, name: str) -> bool:
    kind = node.get("@type")
    return name in kind if isinstance(kind, list) else kind == name


def _text(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("value")
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value if isinstance(item, (str, int, float)))
    if isinstance(value, (int, float)):
        value = str(value)
    return html.unescape(value).strip() or None if isinstance(value, str) else None


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"[0-9][0-9,]*(?:\.\d+)?", value)
        return _amount(match.group(0)) if match else None
    return None


def _json_after(page: str, marker: str) -> object | None:
    """Decode the JSON object assigned after a marker such as ``window.__myx =``."""
    index = page.find(marker)
    if index < 0:
        return None
    start = page.find("{", index + len(marker))
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(page[start:])
    except ValueError:
        return None
    return value


def _put(found: dict, key: str, value: object) -> None:
    if value not in (None, [], "") and key not in found:
        found[key] = value


def _structured_from_json_ld(page: str) -> dict:
    found: dict = {}
    for node in (node for block in _json_blocks(page) for node in _walk(block)):
        if not _is_type(node, "Product") and not _is_type(node, "ProductGroup"):
            continue
        _put(found, "title", _text(node.get("name")))
        _put(found, "brand", _text(node.get("brand")))
        _put(found, "description", _text(node.get("description")))
        _put(found, "color", _text(node.get("color")))
        _put(found, "material", _text(node.get("material")))
        _put(found, "external_id", _text(node.get("sku") or node.get("productID") or node.get("mpn")))
        _put(found, "category", _text(node.get("category")))
        if size := _text(node.get("size")):
            _put(found, "sizes", [size])
        offers = node.get("offers")
        offer_list = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
        for offer in offer_list:
            if not isinstance(offer, dict):
                continue
            price = _number(offer.get("price") or offer.get("lowPrice"))
            if price and "price" not in found:
                found["price"] = price
                found["currency"] = _text(offer.get("priceCurrency"))
            availability = str(offer.get("availability") or "").lower()
            if availability and "availability" not in found:
                found["availability"] = "out_of_stock" if ("outofstock" in availability or "soldout" in availability) else "in_stock"
            spec = offer.get("priceSpecification")
            for item in spec if isinstance(spec, list) else [spec] if isinstance(spec, dict) else []:
                if "list" in str(item.get("priceType", "")).lower() and (mrp := _number(item.get("price"))):
                    _put(found, "mrp", mrp)
        rating = node.get("aggregateRating")
        if isinstance(rating, dict):
            _put(found, "rating", _number(rating.get("ratingValue")))
            count = _number(rating.get("reviewCount") or rating.get("ratingCount"))
            _put(found, "review_count", int(count) if count is not None else None)
        _put(found, "images", _json_ld_images(node))
    return {key: value for key, value in found.items() if value not in (None, [], "")}


def _structured_from_myntra(page: str) -> dict:
    data = _json_after(page, "window.__myx")
    pdp = data.get("pdpData") if isinstance(data, dict) else None
    if not isinstance(pdp, dict):
        return {}
    price = pdp.get("price") if isinstance(pdp.get("price"), dict) else {}
    found: dict = {
        "title": _text(pdp.get("name")),
        "brand": _text(pdp.get("brand")),
        "price": _number(price.get("discounted") or pdp.get("discountedPrice") or price.get("mrp") or pdp.get("mrp")),
        "mrp": _number(price.get("mrp") or pdp.get("mrp")),
        "currency": "INR",
        "color": _text(pdp.get("baseColour")),
        "external_id": _text(pdp.get("id")),
    }
    analytics = pdp.get("analytics") if isinstance(pdp.get("analytics"), dict) else {}
    found["category"] = _text(analytics.get("articleType")) or _text(pdp.get("articleType"))
    found["gender"] = _text(analytics.get("gender")) or _text(pdp.get("gender"))
    attributes = pdp.get("articleAttributes") if isinstance(pdp.get("articleAttributes"), dict) else {}
    found["material"] = _text(attributes.get("Fabric") or attributes.get("Material") or attributes.get("Fabric Type"))
    sizes, unavailable = [], []
    for size in pdp.get("sizes") or []:
        if not isinstance(size, dict) or not (label := _text(size.get("label"))):
            continue
        in_stock = size.get("available")
        if in_stock is None and isinstance(size.get("sizeSellerData"), list):
            in_stock = any((seller or {}).get("availableCount", 0) > 0 for seller in size["sizeSellerData"])
        (sizes if in_stock is not False else unavailable).append(label)
    found["sizes"], found["unavailable_sizes"] = sizes, unavailable
    if sizes or unavailable:
        found["availability"] = "in_stock" if sizes else "out_of_stock"
    ratings = pdp.get("ratings") if isinstance(pdp.get("ratings"), dict) else {}
    found["rating"] = _number(ratings.get("averageRating"))
    count = _number(ratings.get("totalCount"))
    found["review_count"] = int(count) if count is not None else None
    details = pdp.get("productDetails")
    if isinstance(details, list):
        text = " ".join(re.sub(r"<[^>]+>", " ", str((item or {}).get("description") or "")) for item in details if isinstance(item, dict))
        found["description"] = re.sub(r"\s+", " ", html.unescape(text)).strip() or None
    images = []
    media = pdp.get("media") if isinstance(pdp.get("media"), dict) else {}
    for album in media.get("albums") or []:
        for image in (album or {}).get("images") or []:
            if isinstance(image, dict) and (src := image.get("imageURL") or image.get("secureSrc") or image.get("src")):
                images.append(src)
    found["images"] = images
    return {key: value for key, value in found.items() if value not in (None, [], "")}


def _structured_from_amazon(page: str) -> dict:
    found: dict = {}
    if match := re.search(r'id="productTitle"[^>]*>(.*?)</span>', page, re.DOTALL):
        found["title"] = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
    if match := re.search(r'id="bylineInfo"[^>]*>(.*?)</a>', page, re.DOTALL):
        byline = html.unescape(re.sub(r"<[^>]+>|\s+", " ", match.group(1))).strip()
        found["brand"] = re.sub(r"^(?:Visit the\s+|Brand:\s*)|\s+Store$", "", byline).strip() or None
    core = re.search(r'id="(?:corePriceDisplay_desktop_feature_div|corePrice_feature_div|apex_desktop)"(.*?)(?:id="(?:tp_price_block|deliveryBlock|availability))', page, re.DOTALL)
    block = core.group(1) if core else page
    if match := re.search(r'class="a-price[^"]*priceToPay[^"]*"[^>]*>\s*<span class="a-offscreen">\s*([^<]+)<', block) or re.search(r'class="a-offscreen">\s*₹\s*([0-9][^<]*)<', block):
        found["price"] = _number(match.group(1))
    if match := re.search(r'a-text-price[^>]*>\s*<span class="a-offscreen">\s*₹?\s*([0-9][^<]*)<', block):
        found["mrp"] = _number(match.group(1))
    if found.get("price"):
        found["currency"] = "INR"
    variations = _json_after(page, '"variationValues"')
    if isinstance(variations, dict):
        found["sizes"] = [str(size) for size in variations.get("size_name") or [] if size]
        colors = [str(color) for color in variations.get("color_name") or [] if color]
        if colors:
            found["colors"] = colors
    for label, key in (("Material composition", "material"), ("Material type", "material"), ("Material", "material"), ("Colour", "color"), ("Color", "color")):
        if key in found:
            continue
        if match := re.search(rf">\s*{label}\s*</span>\s*</td>\s*<td[^>]*>\s*<span[^>]*>\s*([^<]+)<", page) or re.search(rf"{label}\s*</span>\s*<span[^>]*>\s*:?\s*([^<]+)<", page):
            found[key] = html.unescape(match.group(1)).strip()
    if re.search(r'id="availability".{0,400}?(?:Currently unavailable|out of stock)', page, re.DOTALL | re.IGNORECASE):
        found["availability"] = "out_of_stock"
    return {key: value for key, value in found.items() if value not in (None, [], "")}


def _structured_from_meta(page: str) -> dict:
    def meta(prop: str) -> str | None:
        pattern = re.escape(prop)
        match = re.search(rf"<meta[^>]+(?:property|name)=[\"']{pattern}[\"'][^>]*content=[\"']([^\"']*)", page, re.IGNORECASE) or re.search(
            rf"<meta[^>]+content=[\"']([^\"']*)[\"'][^>]*(?:property|name)=[\"']{pattern}[\"']", page, re.IGNORECASE
        )
        return html.unescape(match.group(1)).strip() if match and match.group(1).strip() else None

    found: dict = {"title": meta("og:title")}
    if not found["title"] and (match := re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)):
        found["title"] = match.group(1)
    found["price"] = _number(meta("product:price:amount") or meta("og:price:amount"))
    found["currency"] = meta("product:price:currency") or meta("og:price:currency")
    found["brand"] = meta("product:brand") or meta("og:brand")
    return {key: value for key, value in found.items() if value not in (None, [], "")}


def _structured_product(page: str, url: str) -> dict:
    """Combine structured product data found in page HTML, most reliable source first."""
    host = (urlsplit(url).hostname or "").lower()
    sources = []
    if "myntra" in host:
        sources.append(_structured_from_myntra(page))
    if "amazon" in host or "amzn" in host:
        sources.append(_structured_from_amazon(page))
    sources += [_structured_from_json_ld(page), _structured_from_meta(page)]
    merged: dict = {}
    for source in sources:
        for key, value in source.items():
            merged.setdefault(key, value)
    if merged.get("title"):
        merged["title"] = _clean_title(merged["title"])
    if merged.get("mrp") and merged.get("price") and merged["mrp"] <= merged["price"]:
        merged.pop("mrp")
    return merged


def _price_from_markdown(markdown: str, title: str) -> tuple[float | None, float | None]:
    """Find the selling price and MRP, preferring the price block near the product title."""
    currency = r"(?:₹|INR|Rs\.?)\s*"
    number = r"([0-9][0-9,]*(?:\.\d{1,2})?)"
    start = markdown.find(title) if title else -1
    region = markdown[start:] if start >= 0 else markdown
    # "₹1471 MRP ₹2299 (36% OFF)" (Myntra) and "₹799 M.R.P.: ₹1,999" (Amazon).
    paired = re.search(rf"{currency}{number}[^0-9₹]{{0,40}}?(?:MRP|M\.R\.P\.?)\s*:?\s*(?:~~)?\s*{currency}{number}", region, re.IGNORECASE)
    if paired:
        price, mrp = _amount(paired.group(1)), _amount(paired.group(2))
        return price, mrp if mrp and price and mrp > price else None
    # "MRP ₹2,299 ₹1,471".
    paired = re.search(rf"(?:MRP|M\.R\.P\.?)\s*:?\s*(?:~~)?\s*{currency}{number}(?:~~)?\s*{currency}{number}", region, re.IGNORECASE)
    if paired:
        mrp, price = _amount(paired.group(1)), _amount(paired.group(2))
        return price, mrp if mrp and price and mrp > price else None
    prices = [amount for raw in re.findall(rf"{currency}{number}", region, re.IGNORECASE) if (amount := _amount(raw)) is not None]
    price = prices[0] if prices else None
    original = next((candidate for candidate in prices[1:3] if price is not None and candidate > price), None)
    return price, original


def _parse_product(markdown: str, url: str) -> ProductData:
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    headings = [re.sub(r"^#+\s*", "", line).strip() for line in lines if re.match(r"^#{1,3}\s+", line)]
    title = headings[0] if headings else (lines[0][:300] if lines else urlsplit(url).hostname or "Product")

    price, original = _price_from_markdown(markdown, title)
    title = _clean_title(title)
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
        outfit_slot=outfit_slot(title),
    )


def _apply_structured(product: ProductData, data: dict, url: str) -> ProductData:
    """Overlay structured page data, which is more reliable than text parsed from markdown."""
    if data.get("title"):
        product.title = data["title"][:500]
    if data.get("price"):
        product.price = Money(amount=data["price"], currency=(data.get("currency") or "INR").upper()[:3])
        mrp = data.get("mrp")
        product.original_price = Money(amount=mrp, currency=product.price.currency) if mrp else None
        product.discount_percent = round((mrp - data["price"]) / mrp * 100, 2) if mrp else None
    for key in ("brand", "description", "category", "material", "external_id", "rating", "review_count", "availability"):
        if data.get(key) is not None:
            setattr(product, key, data[key][:2000] if isinstance(data[key], str) else data[key])
    if data.get("colors") or data.get("color"):
        product.colors = data.get("colors") or [data["color"]]
    if data.get("sizes") or data.get("unavailable_sizes"):
        product.sizes = list(dict.fromkeys(data.get("sizes") or []))
        product.unavailable_sizes = list(dict.fromkeys(data.get("unavailable_sizes") or []))
    images = [image for item in data.get("images") or [] if (image := _normalize_image_url(str(item), url))]
    if images:
        product.image_urls = _rank_images(images + product.image_urls)
    product.outfit_slot = outfit_slot(product.category, product.title) or product.outfit_slot
    return product


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

    async def _fetch_html_via_unlocker(self, url: str) -> str | None:
        """Fetch raw page HTML through the Bright Data Web Unlocker REST API.

        Uses the same API token as the MCP server, whose hosted default zone is
        ``mcp_unlocker``. Store bot walls block the direct fetch from Render.
        """
        token = self.settings.brightdata_api_token.get_secret_value()
        zones = list(dict.fromkeys(zone for zone in (self.settings.brightdata_zone, "mcp_unlocker") if zone))
        async with httpx.AsyncClient(timeout=self.settings.scrape_timeout_seconds) as client:
            for zone in zones:
                response = await client.post(
                    "https://api.brightdata.com/request",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"zone": zone, "url": url, "format": "raw", "country": "in"},
                )
                if response.status_code < 400 and response.text.strip():
                    return response.text
                logger.info(
                    "Bright Data unlocker zone=%s status=%s error=%s",
                    zone,
                    response.status_code,
                    (response.headers.get("x-brd-error") or response.text)[:200].replace(token, "[REDACTED]"),
                )
        return None

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

    async def _fetch_product_html(self, url: str) -> str | None:
        """Page HTML carries structured product data (JSON-LD, store state) that markdown loses."""
        for source, fetch in (
            ("brightdata_unlocker", lambda: self._fetch_html_via_unlocker(url)),
            ("direct", lambda: self._fetch_html_directly(url)),
        ):
            try:
                page = await asyncio.wait_for(fetch(), timeout=self.settings.scrape_timeout_seconds)
            except Exception as exc:
                logger.info("HTML fetch %s failed host=%s type=%s", source, urlsplit(url).hostname, type(exc).__name__)
                continue
            if page and page.strip():
                logger.info("HTML fetch %s host=%s html_bytes=%d", source, urlsplit(url).hostname, len(page))
                return _strip_security_wrapper(page)
        return None

    async def _fallback_images(self, url: str) -> list[str]:
        """Last resort when neither the HTML nor the markdown had product images."""
        try:
            page = await asyncio.wait_for(self._call_brightdata("scrape_as_html", url, optional=True), timeout=self.settings.scrape_timeout_seconds)
        except Exception as exc:
            logger.info("Image fallback brightdata_html failed host=%s type=%s", urlsplit(url).hostname, type(exc).__name__)
            return []
        return _images_from_html(_strip_security_wrapper(page), url) if page else []

    async def scrape(self, url: str, country: str) -> ScrapeResponse:
        del country
        try:
            async with self._semaphore:
                resolved_url = await asyncio.to_thread(_resolve_share_url, url)
                page = await self._fetch_product_html(resolved_url)
                structured = _structured_product(page, resolved_url) if page else {}
                html_images = _images_from_html(page, resolved_url) if page else []
                complete = bool(structured.get("title") and structured.get("price") and (structured.get("images") or html_images))
                content = ""
                if not complete:
                    try:
                        content = _strip_security_wrapper(
                            await asyncio.wait_for(self._fetch_page(resolved_url), timeout=self.settings.scrape_timeout_seconds)
                        )
                    except (ScrapeProviderError, TimeoutError):
                        if not structured.get("title"):
                            raise
                    if "page not found" in content.lower() and len(content) < 500 and not structured.get("title"):
                        raise ScrapeProviderError("The product page was not found")
                product = _parse_product(content, url)
                product = _apply_structured(product, structured, resolved_url)
                if not product.image_urls:
                    product.image_urls = html_images
                if not product.image_urls:
                    product.image_urls = await self._fallback_images(resolved_url)
                logger.info(
                    "Scraped host=%s structured_fields=%s price=%s sizes=%d images=%d",
                    urlsplit(resolved_url).hostname, sorted(structured), product.price.amount, len(product.sizes), len(product.image_urls),
                )
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
