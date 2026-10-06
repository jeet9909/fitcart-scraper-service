import asyncio
import json

import httpx

from app.admin_store import BUCKET, select
from app.config import Settings
from app.tryon import TryOnService


def _service(table_status: int) -> tuple[TryOnService, dict[str, bytes]]:
    service = TryOnService(Settings(brightdata_api_token="t", supabase_url="https://sb.test", supabase_service_role_key="k"))
    objects: dict[str, bytes] = {}

    async def fake_sb(method, url, **kwargs):
        request = httpx.Request(method, url)
        if "/rest/v1/" in url:
            if table_status >= 400:
                return httpx.Response(table_status, json={"code": "PGRST205", "message": "not in schema cache"}, request=request)
            return httpx.Response(200 if method == "GET" else 201, json=[] if method == "GET" else None, request=request)
        if url.endswith("/storage/v1/bucket"):
            return httpx.Response(200, json={"name": BUCKET}, request=request)
        key = url.split(f"/object/{BUCKET}/", 1)[1]
        if method == "GET":
            return httpx.Response(200, content=objects[key], request=request) if key in objects else httpx.Response(400, json={"statusCode": "404"}, request=request)
        objects[key] = kwargs["content"]
        return httpx.Response(200, json={}, request=request)

    service._sb = fake_sb
    return service, objects


def test_admin_tables_fall_back_to_storage_and_survive_a_restart(monkeypatch) -> None:
    monkeypatch.setattr("app.admin_store.SAVE_DELAY_SECONDS", 0)
    service, objects = _service(404)

    async def run():
        saved = await service.rest("POST", "support_tickets", json_body={"email": "a@b.c", "subject": "Hi", "message": "Help"}, prefer="return=representation")
        assert saved.status_code == 201 and saved.json()[0]["status"] == "open"
        await service.rest("POST", "app_settings", params={"on_conflict": "key"}, json_body=[{"key": "maintenance", "value": True}])
        await service.rest("POST", "app_settings", params={"on_conflict": "key"}, json_body=[{"key": "maintenance", "value": False}])
        patched = await service.rest("PATCH", "support_tickets", params={"id": f"eq.{saved.json()[0]['id']}"}, json_body={"status": "resolved"},
                                     prefer="return=representation")
        assert patched.json()[0]["status"] == "resolved"
        await asyncio.sleep(0.05)  # let the backup writes run
        assert set(service.backup.problems) == {"support_tickets", "app_settings"}

    asyncio.run(run())
    assert json.loads(objects["tables/app_settings.json"])[0]["value"] is False  # upsert by key, not a second row

    restarted, _ = _service(404)  # a new server process reading the same storage
    async def fake_sb(method, url, **kwargs):
        request = httpx.Request(method, url)
        if "/rest/v1/" in url:
            return httpx.Response(404, json={}, request=request)
        key = url.split(f"/object/{BUCKET}/", 1)[1]
        return httpx.Response(200, content=objects[key], request=request) if key in objects else httpx.Response(400, json={}, request=request)
    restarted._sb = fake_sb
    rows = asyncio.run(restarted.rest("GET", "support_tickets", params={"status": "eq.resolved"})).json()
    assert len(rows) == 1 and rows[0]["email"] == "a@b.c"


def test_working_tables_are_used_directly() -> None:
    service, objects = _service(200)
    response = asyncio.run(service.rest("POST", "support_tickets", json_body={"email": "a@b.c"}, prefer="return=minimal"))
    assert response.status_code == 201 and not service.backup.problems and "tables/support_tickets.json" not in objects


def test_backup_filters_follow_postgrest_syntax() -> None:
    rows = [{"id": "1", "kind": "look", "created_at": "2026-10-01T10:00:00+00:00", "user_id": None},
            {"id": "2", "kind": "scrape", "created_at": "2026-10-03T10:00:00Z", "user_id": "u"}]
    assert [r["id"] for r in select(rows, {"created_at": "gte.2026-10-02T00:00:00+00:00"})] == ["2"]
    assert [r["id"] for r in select(rows, {"order": "created_at.desc", "limit": "1"})] == ["2"]
    assert [r["id"] for r in select(rows, {"user_id": "is.null"})] == ["1"]
    assert [r["id"] for r in select(rows, {"kind": "in.(look,spin)"})] == ["1"]
