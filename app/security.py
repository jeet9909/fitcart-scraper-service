import asyncio
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpx


class UnsafeUrlError(ValueError):
    pass


def _host_is_allowed(hostname: str, allowed_hosts: tuple[str, ...]) -> bool:
    return not allowed_hosts or any(
        hostname == allowed or hostname.endswith(f".{allowed}") for allowed in allowed_hosts
    )


def validate_public_url(url: str, allowed_hosts: tuple[str, ...] = ()) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrlError("Only absolute HTTP or HTTPS product URLs are accepted")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or not _host_is_allowed(hostname, allowed_hosts):
        raise UnsafeUrlError("The product URL host is not allowed")

    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise UnsafeUrlError("The product URL host could not be resolved") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise UnsafeUrlError("Private, local, and reserved network targets are not allowed")

    return url




class TooLargeError(ValueError):
    pass


MAX_REDIRECTS = 5


async def safe_get(url: str, *, max_bytes: int, headers: dict | None = None, timeout: float = 30,
                   allowed_hosts: tuple[str, ...] = ()) -> httpx.Response:
    """GET a URL someone else gave us without letting it reach our own network: every hop of a redirect is checked
    before it is requested (not only the final address), and the body is read in pieces and cut off at max_bytes
    so a huge file cannot fill the server's memory."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await asyncio.to_thread(validate_public_url, url, allowed_hosts)
            async with client.stream("GET", url, headers=headers) as response:
                if response.is_redirect and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise TooLargeError("The file is too large")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise TooLargeError("The file is too large")
                return httpx.Response(response.status_code, headers=response.headers, content=bytes(body), request=response.request)
    raise UnsafeUrlError("Too many redirects")

# Store Share buttons copy text such as "Check out this shirt on Myntra! https://www.myntra.com/...".
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BARE_URL_IN_TEXT = re.compile(r"\b(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/[^\s<>\"']*", re.IGNORECASE)
_STORE_HOST = re.compile(
    r"(^|\.)(myntra\.com|amazon\.[a-z.]+|amzn\.(in|to|eu)|ajio\.com|flipkart\.com|fkrt\.it|meesho\.com|nike\.com|"
    r"nykaafashion\.com|nykaa\.com|tatacliq\.com|hm\.com|zara\.com|snitch\.co\.in|souledstore\.com|bewakoof\.com)$",
    re.IGNORECASE,
)


def _tidy_store_link(link: str) -> str:
    """Flipkart's Share button gives an app deep link (dl.flipkart.com/dl/...) full of tracking
    parameters; the same product page is www.flipkart.com/... with only its pid."""
    parts = urlsplit(link)
    host = (parts.hostname or "").lower()
    if host == "dl.flipkart.com" and "/p/" in parts.path:
        path = parts.path[3:] if parts.path.startswith("/dl/") else parts.path
        pid = next((pair.split("=", 1)[1] for pair in parts.query.split("&") if pair.startswith("pid=") and "=" in pair), "")
        return f"https://www.flipkart.com{path}" + (f"?pid={pid}" if pid else "")
    if host in ("meesho.com", "www.meesho.com") and "/p/" in parts.path and not parts.path.startswith("/s/"):
        # Meesho product pages need no query string; share links add utm_* and referral tracking.
        return f"https://www.meesho.com{parts.path}"
    return link


def extract_url(text: str) -> str:
    """The product link inside pasted text; text that is already just a link is returned unchanged."""
    text = (text or "").strip()
    if not text or (" " not in text and "\n" not in text and text.lower().startswith(("http://", "https://"))):
        return _tidy_store_link(text) if text else text
    found = _URL_IN_TEXT.findall(text) or _BARE_URL_IN_TEXT.findall(text)
    links = []
    for candidate in found:
        link = candidate.lstrip("<([\"'“‘").rstrip(")]>\"'”’.,;:!?…")
        if not link.lower().startswith(("http://", "https://")):
            link = "https://" + link
        host = urlsplit(link).hostname or ""
        if "." in host:
            links.append((link, host))
    if not links:
        return text
    return _tidy_store_link(next((link for link, host in links if _STORE_HOST.search(host)), links[0][0]))
