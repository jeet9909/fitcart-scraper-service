import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

from app import protect, security
from app.main import app, get_runtime_settings
from tests.test_api import AUTH_SETTINGS


def _server(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_redirects_into_our_network_are_never_followed(monkeypatch) -> None:
    """A public-looking link that redirects to an internal address must not reach it."""
    hits = []

    class Internal(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200); self.end_headers(); self.wfile.write(b"secret")

        def log_message(self, *args):
            pass

    internal = _server(Internal)
    target = f"http://127.0.0.1:{internal.server_port}/metadata"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": target})

    real = httpx.AsyncClient
    monkeypatch.setattr(security.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    public = {"cdn.example": True}
    monkeypatch.setattr(security, "validate_public_url",
                        lambda url, hosts=(): url if httpx.URL(url).host in public else (_ for _ in ()).throw(security.UnsafeUrlError("private")))
    with pytest.raises(security.UnsafeUrlError):
        asyncio.run(security.safe_get("https://cdn.example/shirt.jpg", max_bytes=1000))
    internal.shutdown()
    assert hits == []  # the internal address was checked before any request went to it


def test_downloads_are_cut_off_at_the_size_limit(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    real = httpx.AsyncClient
    monkeypatch.setattr(security.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(security, "validate_public_url", lambda url, hosts=(): url)
    with pytest.raises(security.TooLargeError):
        asyncio.run(security.safe_get("https://cdn.example/huge.jpg", max_bytes=1000))
    assert asyncio.run(security.safe_get("https://cdn.example/ok.jpg", max_bytes=10_000)).content == b"x" * 5000


def test_oversized_uploads_are_refused_before_reading(monkeypatch) -> None:
    monkeypatch.setattr(protect, "MAX_BODY_BYTES", 1000)
    middleware = protect.BodyLimit(app, max_bytes=1000)
    with TestClient(middleware) as client:
        big = client.post("/v1/wardrobe", files={"image": ("a.png", b"0" * 5000, "image/png")})
    assert big.status_code == 413 and "too large" in big.json()["detail"]


def test_security_headers_and_admin_content_policy() -> None:
    with TestClient(app) as client:
        api = client.get("/health")
        console = client.get("/admin/")
    assert api.headers["x-content-type-options"] == "nosniff" and api.headers["x-frame-options"] == "DENY"
    assert "max-age" in api.headers["strict-transport-security"]
    assert "frame-ancestors 'none'" in console.headers["content-security-policy"]


def test_one_address_cannot_spam_reset_emails(monkeypatch) -> None:
    from app import email_auth

    sent = []

    async def fake_send(email, settings, redirect_to=None):
        sent.append(email)

    monkeypatch.setattr(email_auth, "send_reset_code", fake_send)
    settings = AUTH_SETTINGS.model_copy(update={"email_verification_enabled": True})
    app.dependency_overrides[get_runtime_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            codes = [client.post("/v1/auth/password/forgot", json={"email": f"victim{i}@gmail.com"}).status_code for i in range(12)]
            other = client.post("/v1/auth/password/forgot", json={"email": "me@gmail.com"}, headers={"X-Forwarded-For": "203.0.113.9"})
    finally:
        app.dependency_overrides.clear()
    assert codes == [202] * 10 + [429, 429] and len(sent) == 11 and sent[-1] == "me@gmail.com"
    assert other.status_code == 202  # other visitors are not affected
