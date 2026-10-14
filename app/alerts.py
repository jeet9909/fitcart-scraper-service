"""Alerts for the admin console: every failed API request, server crashes and restarts, and high memory.

A middleware records any /v1 request that ends in a server error (5xx or an exception) and Gemini quota
refusals. A crash (for example the server running out of memory) gives no error to record, so each server
also writes a heartbeat every HEARTBEAT_SECONDS to app_settings with its memory use and the looks in progress,
and marks it clean when it shuts down normally (deploys and restarts). A heartbeat that stops without that
mark means the server died; the next server to start, or another one still running, reports it. Recording
never fails or slows down a user request.
"""

import asyncio
import json
import logging
import socket
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Request
from starlette.responses import Response

from app import activity

log = logging.getLogger(__name__)

TABLE = "api_alerts"
INSTANCES_KEY = "server_instances"
HEARTBEAT_SECONDS = 60
DEAD_AFTER_SECONDS = 180  # a heartbeat older than this, without the clean-stop mark, is a crash
DUPLICATE_SECONDS = 60  # the same failure on the same endpoint is recorded once a minute, with a count
MEMORY_WARNING_SHARE = 0.85
MEMORY_WARNING_EVERY_SECONDS = 1800
LABELS = {"look": "Look", "outfit": "Outfit", "spin": "360° view", "pose": "Social pose", "scrape": "Product import"}

INSTANCE = f"{socket.gethostname()}-{uuid4().hex[:6]}"
STARTED = datetime.now(UTC)
_inflight: dict[str, dict[str, Any]] = {}
_recent: dict[tuple[str, str, str], list[float]] = {}  # key -> [last recorded at, repeats since]
_pending: set[asyncio.Task] = set()
_last_memory_warning = 0.0


def _now() -> datetime:
    return datetime.now(UTC)


def _memory_mb() -> tuple[int | None, int | None]:
    """This process's resident memory and the container's memory limit, in MB (None when unknown)."""
    used = None
    try:
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    used = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass
    from app.tryon import _memory_limit  # the 4K guard reads the same limit

    limit = _memory_limit()
    return used, limit // 2**20 if limit else None


def record(app, *, kind: str, title: str, detail: str | None = None, severity: str = "error", method: str | None = None,
           path: str | None = None, http_status: int | None = None, user_id: str | None = None, email: str | None = None,
           meta: dict | None = None) -> None:
    """Save one alert in the background. Repeats of the same failure within a minute are folded into the next one."""
    key = (kind, title, path or "")
    now = time.monotonic()
    seen = _recent.get(key)
    if seen and now - seen[0] < DUPLICATE_SECONDS:
        seen[1] += 1
        return
    repeats = seen[1] if seen else 0
    _recent[key] = [now, 0]
    if len(_recent) > 500:
        for old in sorted(_recent, key=lambda k: _recent[k][0])[:250]:
            _recent.pop(old, None)
    row = {"kind": kind, "severity": severity, "title": title[:200], "detail": (detail or "")[:1000] or None, "method": method,
           "path": path, "http_status": http_status, "user_id": user_id, "email": email,
           "meta": {**(meta or {}), "instance": INSTANCE, **({"repeats_before": repeats} if repeats else {})}}
    log.log(logging.ERROR if severity == "error" else logging.WARNING if severity == "warning" else logging.INFO,
            "Alert: %s%s", title, f" ({detail})" if detail else "")
    try:
        task = asyncio.get_running_loop().create_task(_save(app, row))
    except RuntimeError:
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _save(app, row: dict) -> None:
    service = getattr(app.state, "tryon", None)
    if service is None or not service.settings.supabase_url:
        return
    try:
        response = await service.rest("POST", TABLE, json_body=row, prefer="return=minimal")
        if response.status_code >= 400:
            log.warning("Could not save an alert: %s %s", response.status_code, response.text[:200])
    except Exception:
        log.warning("Could not save an alert", exc_info=True)


# ---------------------------------------------------------------- failed requests


def _detail_of(body: bytes) -> tuple[str | None, str | None]:
    """The message and code from a FastAPI error body."""
    try:
        detail = json.loads(body or b"{}").get("detail")
    except (ValueError, AttributeError):
        return (body[:300].decode("utf-8", "replace") or None), None
    if isinstance(detail, dict):
        message = detail.get("message") or str(detail)[:300]
        if detail.get("reason"):  # the cause behind a friendly message, for the admin only
            message = f"{message} — {detail['reason']}"
        return message, detail.get("code")
    if isinstance(detail, list):
        return str(detail)[:300], None
    return (str(detail) if detail else None), None


async def middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/v1/") or path.startswith("/v1/admin/alerts"):
        return await call_next(request)
    kind = activity.tracked_kind(request.method, path)
    request_id = uuid4().hex
    if kind:
        claims = activity._claims(request, request.app.state.settings)
        _inflight[request_id] = {"kind": kind, "email": claims.get("email"), "started": time.time()}
    try:
        response = await call_next(request)
    except Exception as exc:
        claims = activity._claims(request, request.app.state.settings)
        record(request.app, kind="request_failed", title=f"{LABELS.get(kind, 'API request')} crashed", detail=f"{type(exc).__name__}: {exc}",
               method=request.method, path=path, http_status=500, user_id=claims.get("sub"), email=claims.get("email"))
        raise
    finally:
        _inflight.pop(request_id, None)
    status = response.status_code
    if status < 500 and status != 429:
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    message, code = _detail_of(body)
    rebuilt = Response(content=body, status_code=status, headers=dict(response.headers), media_type=response.media_type)
    if code == "maintenance" or (status == 429 and "gemini" not in (message or "").lower()):
        return rebuilt  # maintenance mode and shoppers' own rate limits are not failures
    claims = activity._claims(request, request.app.state.settings)
    what = LABELS.get(kind, "API request")
    title = f"Gemini quota hit: {what.lower()} refused" if status == 429 else f"{what} failed ({status})"
    record(request.app, kind="quota" if status == 429 else "request_failed", title=title, detail=message, method=request.method,
           path=path, http_status=status, user_id=claims.get("sub"), email=claims.get("email"))
    return rebuilt


# ---------------------------------------------------------------- heartbeat and crash detection


async def _instances(service) -> dict[str, dict]:
    response = await service.rest("GET", "app_settings", params={"select": "key,value", "key": f"eq.{INSTANCES_KEY}"})
    if response.status_code >= 400:
        return {}
    rows = response.json()
    value = rows[0].get("value") if rows else None
    return dict(value) if isinstance(value, dict) else {}


async def _save_instances(service, value: dict[str, dict]) -> None:
    await service.rest("POST", "app_settings", params={"on_conflict": "key"},
                       json_body=[{"key": INSTANCES_KEY, "value": value, "updated_at": _now().isoformat()}],
                       prefer="resolution=merge-duplicates,return=minimal")


def _beat(clean: bool = False) -> dict:
    used, limit = _memory_mb()
    now = time.time()
    return {"at": _now().isoformat(), "started": STARTED.isoformat(), "clean": clean, "memory_mb": used, "limit_mb": limit,
            "inflight": [{"kind": job["kind"], "email": job.get("email"), "seconds": round(now - job["started"])} for job in _inflight.values()][:20]}


def _crash_detail(beat: dict) -> str:
    seen = beat.get("at", "")
    try:
        seen = datetime.fromisoformat(seen).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b %H:%M:%S IST")
    except (ValueError, TypeError):
        pass
    jobs = beat.get("inflight") or []
    memory = beat.get("memory_mb")
    limit = beat.get("limit_mb")
    parts = [f"The server stopped without shutting down. Last heard from at {seen}."]
    if memory:
        parts.append(f"Memory then: {memory} MB" + (f" of {limit} MB" if limit else "") + ".")
    if limit and memory and memory >= limit * 0.7:
        parts.append("It was close to its memory limit, so it most likely ran out of memory.")
    elif not jobs:
        parts.append("Usually this means it ran out of memory or the host restarted it.")
    if jobs:
        listed = ", ".join(f"{LABELS.get(j.get('kind'), j.get('kind'))}{' for ' + j['email'] if j.get('email') else ''} ({j.get('seconds', 0)} s)" for j in jobs)
        parts.append(f"{len(jobs)} request{'s were' if len(jobs) != 1 else ' was'} in progress and failed for the user: {listed}.")
    return " ".join(parts)


async def _check_others(app, service, starting: bool) -> None:
    instances = await _instances(service)
    now = _now()
    for name, beat in list(instances.items()):
        if name == INSTANCE:
            continue
        try:
            age = (now - datetime.fromisoformat(beat.get("at"))).total_seconds()
        except (TypeError, ValueError):
            age = DEAD_AFTER_SECONDS + 1
        if beat.get("clean"):
            if starting:
                record(app, kind="server_restart", severity="info", title="Server restarted",
                       detail="The previous server shut down normally (a deploy or a planned restart).", meta={"previous": name})
            instances.pop(name)
        elif age > DEAD_AFTER_SECONDS:
            record(app, kind="server_crash", title="Server crashed and restarted", detail=_crash_detail(beat),
                   meta={"previous": name, **{k: beat.get(k) for k in ("memory_mb", "limit_mb", "inflight", "at")}})
            instances.pop(name)
    instances[INSTANCE] = _beat()
    await _save_instances(service, instances)


def _check_memory(app) -> None:
    global _last_memory_warning
    used, limit = _memory_mb()
    if not used or not limit or used < limit * MEMORY_WARNING_SHARE:
        return
    if time.monotonic() - _last_memory_warning < MEMORY_WARNING_EVERY_SECONDS:
        return
    _last_memory_warning = time.monotonic()
    record(app, kind="memory", severity="warning", title="Server memory is almost full",
           detail=f"Using {used} MB of {limit} MB. If it runs out, the server restarts and looks in progress fail. "
                  "A bigger instance (2 GB) fixes this.", meta={"memory_mb": used, "limit_mb": limit, "inflight": len(_inflight)})


async def watch(app) -> None:
    """Runs for the server's lifetime: heartbeat, crash reports for servers that died, memory warnings."""
    service = app.state.tryon
    if not service.settings.supabase_url:
        return
    starting = True
    while True:
        try:
            await _check_others(app, service, starting)
            starting = False
            _check_memory(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Heartbeat failed", exc_info=True)
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def stopped(app) -> None:
    """Mark this server's heartbeat as a clean stop, so the restart is not reported as a crash."""
    service = getattr(app.state, "tryon", None)
    if service is None or not service.settings.supabase_url:
        return
    try:
        instances = await _instances(service)
        instances[INSTANCE] = _beat(clean=True)
        await _save_instances(service, instances)
    except Exception:
        log.warning("Could not mark the clean shutdown", exc_info=True)
    if _pending:
        await asyncio.wait(list(_pending), timeout=5)
