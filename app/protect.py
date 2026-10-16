"""Request-level protections: a cap on request size, security headers, and per-address limits for sign-in actions.

The size cap runs before anything reads the body, so an oversized upload is refused at once instead of being
written to disk and read into memory. The headers stop the admin console being framed by another site and tell
browsers to keep to HTTPS and to trust the declared content types. The address limits stop one address from
spamming sign-up, sign-in, reset and verification emails (which would also use up the Gmail sending quota).
"""

import time

from fastapi import HTTPException, Request, status

MAX_BODY_BYTES = 60 * 1024 * 1024  # a person photo plus up to five piece photos, 10 MB each

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "DENY",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}
ADMIN_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
             "font-src https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'; "
             "base-uri 'none'; form-action 'self'; object-src 'none'")


class BodyLimit:
    """ASGI middleware: 413 for bodies over MAX_BODY_BYTES, whether or not the client declares the length."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            return await self._too_large(send)
        seen = 0

        async def limited():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge()
            return message

        try:
            await self.app(scope, limited, send)
        except _TooLarge:
            await self._too_large(send)

    async def _too_large(self, send) -> None:
        body = b'{"detail":"The upload is too large. Use photos under 10 MB each."}'
        await send({"type": "http.response.start", "status": 413,
                     "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class _TooLarge(Exception):
    pass


async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if request.url.path.startswith("/admin"):
        response.headers["content-security-policy"] = ADMIN_CSP
        response.headers["cache-control"] = "no-store"
    return response


# ---------------------------------------------------------------- per-address limits

_hits: dict[tuple[str, str], list[float]] = {}
AUTH_LIMITS = {  # bucket: (requests, window in seconds) per address
    "signup": (20, 3600),
    "login": (40, 600),
    "email": (10, 600),  # resend, forgot password: each sends an email
    "link": (40, 600),  # trading email/Google links, codes, new passwords
}


def client_address(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")  # the proxy puts the visitor's address first
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def limit(request: Request, bucket: str, key: str | None = None) -> None:
    """Allow the request, or raise 429 when this address (or key) used up the bucket's allowance."""
    count, window = AUTH_LIMITS[bucket]
    now, who = time.monotonic(), (bucket, key or client_address(request))
    recent = [t for t in _hits.get(who, []) if now - t < window]
    if len(recent) >= count:
        _hits[who] = recent
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail={"code": "too_many_requests", "message": "Too many attempts. Please wait a few minutes and try again."})
    _hits[who] = [*recent, now]
    if len(_hits) > 50_000:  # forget quiet addresses so the table cannot grow without end
        for old in [k for k, times in _hits.items() if not times or now - times[-1] > 3600][:25_000]:
            _hits.pop(old, None)
