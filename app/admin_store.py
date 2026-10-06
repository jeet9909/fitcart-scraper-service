"""Backup storage for the admin tables (activity_events, support_tickets, app_settings, admin_audit).

The admin console depends on four tables from supabase/schema.sql. When Supabase's REST API cannot use them
(the SQL was not run, PostgREST's schema cache is stale, grants are missing), every admin feature would stop.
Instead, rows go to a private Supabase Storage bucket the server creates for itself, one JSON file per table,
and reads merge both places. Storage needs no SQL and no schema cache, so maintenance mode, support tickets,
the audit log and activity keep working. If storage fails too, rows are kept in memory until the next restart.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

log = logging.getLogger(__name__)

BACKED_UP_TABLES = ("activity_events", "support_tickets", "app_settings", "admin_audit")
BUCKET = "mydripcheck-admin"
MAX_ROWS = {"activity_events": 5000, "admin_audit": 500, "support_tickets": 5000, "app_settings": 100}
SAVE_DELAY_SECONDS = 2.0
DEFAULTS: dict[str, dict[str, Any]] = {
    "activity_events": {"image_calls": 0, "vertex_calls": 0, "cost_inr": 0, "meta": {}},
    "support_tickets": {"status": "open", "priority": "normal", "note": None},
    "admin_audit": {"detail": {}},
    "app_settings": {},
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _compare(left: Any, right: str) -> tuple[Any, Any]:
    a, b = _time(left), _time(right)
    if a and b:
        return a, b
    try:
        return float(left), float(right)
    except (TypeError, ValueError):
        return str(left), right


def matches(row: dict, key: str, condition: str) -> bool:
    """PostgREST's filter syntax (eq., gte., in.(...), is.null, not.is.null), applied to one row."""
    value = row.get(key)
    if condition in ("is.null", "not.is.null"):
        return (value is None) == (condition == "is.null")
    op, _, arg = condition.partition(".")
    if op == "in":
        return str(value) in {item.strip().strip('"') for item in arg.strip("()").split(",")}
    if value is None:
        return False
    a, b = _compare(value, arg)
    try:
        return {"eq": a == b, "neq": a != b, "gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}.get(op, True)
    except TypeError:
        return False


def select(rows: list[dict], params: dict[str, str] | None) -> list[dict]:
    params = params or {}
    filters = [(k, v) for k, v in params.items() if k not in ("select", "order", "limit", "offset", "on_conflict")]
    out = [row for row in rows if all(matches(row, k, v) for k, v in filters)]
    if order := params.get("order"):
        column, _, direction = order.partition(".")
        out.sort(key=lambda row: _time(row.get(column)) or str(row.get(column) or ""), reverse=direction.startswith("desc"))
    if limit := params.get("limit"):
        out = out[: int(limit)]
    return out


def _response(method: str, table: str, status: int, body: Any = None) -> httpx.Response:
    request = httpx.Request(method, f"https://backup.local/{table}")
    if body is None:
        return httpx.Response(status, request=request)
    return httpx.Response(status, json=body, request=request)


class BackupTables:
    """Holds the backup copy of the admin tables and answers REST calls when Supabase's table API cannot."""

    def __init__(self, service) -> None:
        self.service = service
        self._rows: dict[str, list[dict]] = {}
        self._locks = {table: asyncio.Lock() for table in BACKED_UP_TABLES}
        self._saves: dict[str, asyncio.Task] = {}
        self._bucket_ready = False
        self.problems: dict[str, str] = {}  # table -> what Supabase said the last time the table failed
        self.storage_error: str | None = None

    # ---------------------------------------------------------------- storage

    def _url(self, path: str = "") -> str:
        return f"{self.service.settings.supabase_url.rstrip('/')}/storage/v1/{path}"

    async def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        response = await self.service._sb("POST", self._url("bucket"), headers={**self.service._headers, "Content-Type": "application/json"},
                                          json={"id": BUCKET, "name": BUCKET, "public": False})
        if response.status_code < 400 or "already exists" in response.text.lower() or response.status_code == 409:
            self._bucket_ready = True
            return
        raise RuntimeError(f"Could not create the {BUCKET} bucket: {response.status_code} {response.text[:200]}")

    async def _load(self, table: str) -> list[dict]:
        if table in self._rows:
            return self._rows[table]
        rows: list[dict] = []
        try:
            response = await self.service._sb("GET", self._url(f"object/{BUCKET}/tables/{table}.json"), headers=self.service._headers)
            if response.status_code < 400:
                loaded = json.loads(response.content or b"[]")
                rows = loaded if isinstance(loaded, list) else []
            elif response.status_code not in (400, 404):
                self.storage_error = f"{response.status_code} {response.text[:200]}"
        except Exception as exc:  # storage down: run from memory
            self.storage_error = repr(exc)[:200]
            log.warning("Backup storage read failed for %s: %r", table, exc)
        self._rows[table] = rows
        return rows

    async def _write(self, table: str) -> None:
        await asyncio.sleep(SAVE_DELAY_SECONDS)  # several rows in quick succession become one upload
        self._saves.pop(table, None)
        body = json.dumps(self._rows.get(table, []), default=str).encode()
        try:
            await self._ensure_bucket()
            response = await self.service._sb("POST", self._url(f"object/{BUCKET}/tables/{table}.json"),
                                              headers={**self.service._headers, "Content-Type": "application/json", "x-upsert": "true"}, content=body)
            if response.status_code >= 400:
                raise RuntimeError(f"{response.status_code} {response.text[:200]}")
            self.storage_error = None
        except Exception as exc:
            self.storage_error = str(exc)[:200]
            log.warning("Backup storage write failed for %s (kept in memory): %s", table, exc)

    def _schedule_save(self, table: str) -> None:
        if table not in self._saves:
            self._saves[table] = asyncio.create_task(self._write(table))

    async def flush(self) -> None:
        """Write pending changes now (tests and shutdown)."""
        for task in list(self._saves.values()):
            task.cancel()
        self._saves.clear()
        for table in list(self._rows):
            try:
                await self._ensure_bucket()
                await self.service._sb("POST", self._url(f"object/{BUCKET}/tables/{table}.json"),
                                       headers={**self.service._headers, "Content-Type": "application/json", "x-upsert": "true"},
                                       content=json.dumps(self._rows[table], default=str).encode())
            except Exception:
                log.warning("Backup flush failed for %s", table, exc_info=True)

    # ---------------------------------------------------------------- table operations

    async def insert(self, table: str, items: list[dict], on_conflict: str | None = None) -> list[dict]:
        async with self._locks[table]:
            rows = await self._load(table)
            saved = []
            for item in items:
                row = {**DEFAULTS.get(table, {}), **item}
                row.setdefault("id", str(uuid4()))
                row.setdefault("created_at", _now())
                if table == "support_tickets":
                    row.setdefault("updated_at", row["created_at"])
                existing = next((r for r in rows if on_conflict and r.get(on_conflict) == row.get(on_conflict)), None)
                if existing is not None:
                    existing.update(row)
                    saved.append(existing)
                else:
                    rows.append(row)
                    saved.append(row)
            if len(rows) > MAX_ROWS.get(table, 5000):
                rows.sort(key=lambda r: _time(r.get("created_at")) or datetime.min.replace(tzinfo=UTC))
                del rows[: len(rows) - MAX_ROWS.get(table, 5000)]
            self._schedule_save(table)
            return [dict(row) for row in saved]

    async def update(self, table: str, params: dict[str, str] | None, changes: dict) -> list[dict]:
        async with self._locks[table]:
            hit = select(await self._load(table), {k: v for k, v in (params or {}).items() if k not in ("limit", "order")})
            for row in hit:
                row.update(changes)
            if hit:
                self._schedule_save(table)
            return [dict(row) for row in hit]

    async def rows(self, table: str, params: dict[str, str] | None = None) -> list[dict]:
        return [dict(row) for row in select(await self._load(table), params)]

    def has_rows(self, table: str) -> bool:
        return bool(self._rows.get(table))

    # ---------------------------------------------------------------- the REST call, with the backup behind it

    async def rest(self, method: str, table: str, *, params: dict[str, str] | None, json_body: Any, prefer: str | None,
                   real) -> httpx.Response:
        """Try Supabase's table first; use the backup when it fails, and merge backup rows into reads."""
        try:
            response = await real()
            failed = response.status_code >= 400
            reason = f"{response.status_code} {response.text[:200]}" if failed else ""
        except Exception as exc:
            response, failed, reason = None, True, repr(exc)[:200]
        if not failed:
            self.problems.pop(table, None)
        else:
            if table not in self.problems:
                log.warning("Supabase table %s is not usable (%s); using backup storage", table, reason)
            self.problems[table] = reason
        representation = "return=representation" in (prefer or "")
        if method == "GET":
            backup = await self.rows(table, params)  # loaded from storage once per process, then from memory
            if not failed and not backup:
                return response
            live = [] if failed else response.json()
            seen = {str(row.get("id") or row.get("key")) for row in live}
            merged = live + [row for row in backup if str(row.get("id") or row.get("key")) not in seen]
            return _response("GET", table, 200, select(merged, {k: v for k, v in (params or {}).items() if k in ("order", "limit")}))
        if method == "POST":
            items = json_body if isinstance(json_body, list) else [json_body]
            if not failed:
                if table == "app_settings":  # keep a copy, so settings survive if the table breaks later
                    await self.insert(table, items, on_conflict="key")
                return response
            saved = await self.insert(table, items, on_conflict=(params or {}).get("on_conflict"))
            return _response("POST", table, 201, saved if representation else None)
        if method == "PATCH":
            if not failed and (not representation or response.json()):
                if table in self._rows:
                    await self.update(table, params, json_body or {})
                return response
            updated = await self.update(table, params, json_body or {})
            return _response("PATCH", table, 200, updated if representation else None)
        return response if response is not None else _response(method, table, 502, {"message": reason})

    def status(self) -> dict:
        return {"tables_on_backup": dict(self.problems), "storage_error": self.storage_error, "bucket": BUCKET}
