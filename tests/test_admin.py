import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import httpx

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("BRIGHTDATA_API_TOKEN", "test")

from fastapi.testclient import TestClient

from app import admin, email_auth
from app.config import Settings
from app.main import app, get_runtime_settings, get_scraper
from app.scraper import ScrapeProviderError
from app.tryon import GeminiUsage

SECRET = "a-secure-test-secret-that-is-long-enough"
NOW = datetime.now(UTC)
USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"


def _settings(**extra) -> Settings:
    return Settings(brightdata_api_token="test", anonymous_token_secret=SECRET, supabase_url="https://sb.test",
                    supabase_service_role_key="service", ADMIN_EMAILS="boss@mydripcheck.com", **extra)


class FakeService:
    """Stands in for TryOnService.rest: tables are lists of rows; writes are recorded."""

    def __init__(self, settings: Settings, tables: dict[str, list[dict]] | None = None) -> None:
        self.settings = settings
        self.tables = tables or {}
        self.writes: list[tuple[str, str, object]] = []
        self.usage = GeminiUsage()

    async def rest(self, method, table, *, params=None, json_body=None, prefer=None):
        request = httpx.Request(method, f"https://sb.test/rest/v1/{table}")
        if method != "GET":
            self.writes.append((method, table, json_body))
            body = json_body if isinstance(json_body, list) else [{"id": "t-1", **(json_body or {})}]
            return httpx.Response(201, json=body, request=request)
        if table not in self.tables:
            return httpx.Response(404, json={"message": "missing"}, request=request)
        rows = self.tables[table]
        for key, value in (params or {}).items():
            if isinstance(value, str) and value.startswith("eq.") and key not in ("select",):
                rows = [row for row in rows if str(row.get(key)) == value[3:]]
        return httpx.Response(200, json=rows, request=request)


AUTH_USERS = [
    {"id": USER_A, "email": "asha@example.com", "created_at": (NOW - timedelta(days=2)).isoformat(), "last_sign_in_at": NOW.isoformat()},
    {"id": USER_B, "email": "boss@mydripcheck.com", "created_at": (NOW - timedelta(days=40)).isoformat(), "last_sign_in_at": None},
]


def _fake_auth(monkeypatch, calls: list | None = None) -> None:
    async def fake(settings, method, path, *, json=None, params=None):
        if calls is not None:
            calls.append((method, path, json))
        request = httpx.Request(method, f"https://sb.test/auth/v1{path}")
        if path == "/token":
            user = next((u for u in AUTH_USERS if u["email"] == json["email"]), None)
            if user is None or json["password"] != "right-password":
                return httpx.Response(400, json={"msg": "bad"}, request=request)
            return httpx.Response(200, json={"user": user}, request=request)
        if path == "/admin/users" and method == "GET":
            return httpx.Response(200, json={"users": AUTH_USERS}, request=request)
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(email_auth, "_auth_request", fake)


def _client(monkeypatch, service: FakeService):
    settings = service.settings
    _fake_auth(monkeypatch)
    app.dependency_overrides[admin.get_settings] = lambda: settings
    app.dependency_overrides[admin.get_service] = lambda: service
    app.dependency_overrides[get_runtime_settings] = lambda: settings
    state = admin.AdminState(service)
    app.dependency_overrides[admin.get_state] = lambda: state
    return state


def _login(client) -> dict:
    response = client.post("/v1/admin/session", json={"email": "boss@mydripcheck.com", "password": "right-password"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_admin_dashboard_page_is_served_separately() -> None:
    with TestClient(app) as client:
        page = client.get("/admin/")
        assert page.status_code == 200 and "Admin Studio" in page.text
        assert "Admin Studio" not in client.get("/").text


def test_only_admin_emails_get_an_admin_session(monkeypatch) -> None:
    service = FakeService(_settings())
    _client(monkeypatch, service)
    try:
        with TestClient(app) as client:
            assert client.get("/v1/admin/me").status_code == 401
            wrong = client.post("/v1/admin/session", json={"email": "boss@mydripcheck.com", "password": "nope"})
            assert wrong.status_code == 401
            not_admin = client.post("/v1/admin/session", json={"email": "asha@example.com", "password": "right-password"})
            assert not_admin.status_code == 403
            headers = _login(client)
            assert client.get("/v1/admin/me", headers=headers).json() == {"email": "boss@mydripcheck.com"}
            # A normal shopper session is not an admin session.
            from app.anonymous_auth import create_email_session
            shopper = create_email_session(USER_A, "asha@example.com", service.settings).access_token
            assert client.get("/v1/admin/me", headers={"Authorization": f"Bearer {shopper}"}).status_code == 401
            assert client.get("/v1/admin/me", headers={"X-Admin-Token": "x"}).status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_users_show_plan_looks_left_and_status(monkeypatch) -> None:
    month = NOW.astimezone(admin.IST).strftime("%Y-%m")
    service = FakeService(_settings(), {
        "look_grants": [
            {"user_id": USER_A, "kind": "free", "looks": 2, "used": 1, "period": month},
            {"user_id": USER_A, "kind": "plus", "looks": 18, "used": 3, "period": None},
        ],
        "try_on_gallery": [{"anonymous_user_id": USER_A}, {"anonymous_user_id": USER_A}],
        "app_settings": [{"key": "suspended", "value": [USER_B]}],
    })
    _client(monkeypatch, service)
    try:
        with TestClient(app) as client:
            body = client.get("/v1/admin/users", headers=_login(client)).json()
        asha = next(u for u in body["users"] if u["id"] == USER_A)
        assert asha["plan"] == "plus" and asha["looks_left"] == 16 and asha["looks_made"] == 2 and asha["status"] == "active"
        boss = next(u for u in body["users"] if u["id"] == USER_B)
        assert boss["plan"] == "free" and boss["looks_left"] == 2 and boss["status"] == "suspended" and boss["admin"]
        assert body["paying"] == 1 and body["new_this_week"] == 1 and body["suspended"] == 1
    finally:
        app.dependency_overrides.clear()


def test_admin_can_add_bonus_looks_and_it_is_audited(monkeypatch) -> None:
    service = FakeService(_settings())
    _client(monkeypatch, service)
    try:
        with TestClient(app) as client:
            response = client.post(f"/v1/admin/users/{USER_A}/looks", json={"looks": 5, "days": 10, "reason": "Sorry for the failed look"},
                                   headers=_login(client))
        assert response.status_code == 200 and response.json()["added"] == 5
        grant = next(body for method, table, body in service.writes if table == "look_grants")
        assert grant["kind"] == "bonus" and grant["looks"] == 5 and grant["payment_ref"].startswith("admin:")
        audit = next(body for method, table, body in service.writes if table == "admin_audit")
        assert audit["action"] == "add_looks" and audit["admin_email"] == "boss@mydripcheck.com"
    finally:
        app.dependency_overrides.clear()


def test_revenue_counts_each_payment_once_and_skips_admin_gifts(monkeypatch) -> None:
    day = (NOW - timedelta(days=1)).isoformat()
    grants = [{"user_id": USER_A, "kind": "pass", "looks": 7, "payment_ref": "order_p", "created_at": day},
              {"user_id": USER_A, "kind": "plus", "looks": 18, "payment_ref": "order_m:0", "created_at": day}]
    grants += [{"user_id": USER_A, "kind": "pro", "looks": 40, "payment_ref": f"order_y:{i}", "created_at": day} for i in range(12)]
    service = FakeService(_settings(), {"look_grants": grants, "activity_events": []})
    _client(monkeypatch, service)
    try:
        with TestClient(app) as client:
            body = client.get("/v1/admin/revenue?days=30", headers=_login(client)).json()
        assert body["gross_inr"] == 129 + 349 + 7499
        assert {p["billing"] for p in body["purchases"]} == {"once", "monthly", "yearly"}
        assert body["test_mode"] is True
    finally:
        app.dependency_overrides.clear()


def test_maintenance_mode_pauses_try_ons_with_a_clear_message(monkeypatch) -> None:
    service = FakeService(_settings(), {"app_settings": [{"key": "maintenance", "value": True}]})
    with TestClient(app) as client:
        app.state.settings = service.settings
        app.state.admin_state = admin.AdminState(service)
        response = client.post("/v1/try-ons", files={"person_image": ("me.png", b"x", "image/png")})
        site = client.get("/v1/site").json()
    app.state.settings = Settings(brightdata_api_token="test")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "maintenance"
    assert site["maintenance"] is True and site["maintenance_message"]


def test_failed_product_import_is_logged_with_its_store(monkeypatch) -> None:
    class FailingScraper:
        async def scrape(self, url, country):
            raise ScrapeProviderError("Store blocked the request")

    service = FakeService(_settings(ALLOWED_PRODUCT_HOSTS=""), {"app_settings": []})
    monkeypatch.setattr("app.main.validate_public_url", lambda url, hosts=(): url)
    app.dependency_overrides[get_scraper] = lambda: FailingScraper()
    app.dependency_overrides[get_runtime_settings] = lambda: service.settings
    try:
        with TestClient(app) as client:
            app.state.settings = service.settings
            app.state.tryon = service
            app.state.admin_state = admin.AdminState(service)
            response = client.post("/v1/products/scrape", json={"url": "https://www.myntra.com/shirts/123"})
            for _ in range(20):
                if any(table == "activity_events" for _, table, _ in service.writes):
                    break
                asyncio.run(asyncio.sleep(0.01))
    finally:
        app.dependency_overrides.clear()
        app.state.settings = Settings(brightdata_api_token="test")
    assert response.status_code == 502
    event = next(body for method, table, body in service.writes if table == "activity_events")
    assert event["kind"] == "scrape" and event["status"] == "failed" and event["meta"]["store"] == "Myntra"
    assert "blocked" in event["error"]


def test_support_ticket_from_the_storefront(monkeypatch) -> None:
    service = FakeService(_settings())
    _client(monkeypatch, service)
    admin._ticket_times.clear()
    try:
        with TestClient(app) as client:
            missing_email = client.post("/v1/support", json={"subject": "Help", "message": "My look failed twice"})
            created = client.post("/v1/support", json={"email": "Asha@Example.com", "subject": "Payment done", "message": "I paid but looks missing"})
    finally:
        app.dependency_overrides.clear()
    assert missing_email.status_code == 422
    assert created.status_code == 201
    ticket = next(body for method, table, body in service.writes if table == "support_tickets")
    assert ticket["email"] == "asha@example.com" and ticket["priority"] == "high" and ticket["status"] == "open"


def test_activity_cost_uses_counted_model_calls() -> None:
    from app.activity import estimated_cost

    settings = _settings()
    assert estimated_cost({"gemini_image": 2, "vertex": 1}, settings) == round(2 * 6.40 + 5.30, 2)
    assert json.dumps(admin.DEFAULT_SETTINGS)  # settings stay JSON for the app_settings table
