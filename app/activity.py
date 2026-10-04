"""Activity log for the admin dashboard: one row per try-on, 360° view, social pose and product import.

A middleware wraps those endpoints, counts the Gemini and Vertex calls each request makes (through a
context variable that the try-on service bumps), and saves the outcome to the activity_events table after
the response has been sent. Logging never fails or slows down a user request.
"""

import asyncio
import logging
import re
import time
from contextvars import ContextVar
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.anonymous_auth import verify_session_token
from app.config import Settings

log = logging.getLogger(__name__)

_current: ContextVar[dict[str, Any] | None] = ContextVar("activity", default=None)

# Requests worth logging, by method and path.
TRACKED: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"^/v1/try-ons/outfit/?$"), "outfit"),
    ("POST", re.compile(r"^/v1/try-ons/[^/]+/spin/?$"), "spin"),
    ("POST", re.compile(r"^/v1/try-ons/[^/]+/poses/?$"), "pose"),
    ("POST", re.compile(r"^/v1/try-ons/?$"), "look"),
    ("POST", re.compile(r"^/v1/products/scrape/?$"), "scrape"),
)
GENERATION_KINDS = ("look", "outfit", "spin", "pose")
# Refusals that are not failures of the service: signed out, no looks left, plan required, rate limited.
REJECTED_STATUSES = {401, 402, 403, 409, 429}


def tracked_kind(method: str, path: str) -> str | None:
    return next((kind for verb, pattern, kind in TRACKED if verb == method and pattern.match(path)), None)


def count_call(provider: str) -> None:
    """Called by the try-on service for every billable model call ('gemini_image', 'gemini_text' or 'vertex')."""
    current = _current.get()
    if current is not None:
        current["calls"][provider] = current["calls"].get(provider, 0) + 1


def note(**fields: Any) -> None:
    """Extra details for the current request's activity row (store, pose, pieces)."""
    current = _current.get()
    if current is not None:
        current["meta"].update({key: value for key, value in fields.items() if value is not None})


def note_error(message: Any) -> None:
    current = _current.get()
    if current is not None and message:
        if isinstance(message, dict):
            message = message.get("message") or message.get("code") or str(message)
        current["error"] = str(message)[:500]


def estimated_cost(calls: dict[str, int], settings: Settings) -> float:
    return round(
        calls.get("gemini_image", 0) * settings.cost_gemini_image_inr
        + calls.get("vertex", 0) * settings.cost_vertex_tryon_inr
        + calls.get("gemini_text", 0) * settings.cost_gemini_text_inr,
        2,
    )


def _claims(request: Request, settings: Settings) -> dict:
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        return {}
    try:
        return verify_session_token(token, settings)
    except Exception:  # an invalid token is reported by the endpoint itself
        return {}


async def middleware(request: Request, call_next):
    kind = tracked_kind(request.method, request.url.path)
    if kind is None:
        return await call_next(request)
    app = request.app
    settings: Settings = app.state.settings
    claims = _claims(request, settings)
    admin_state = getattr(app.state, "admin_state", None)
    if admin_state is not None and kind in GENERATION_KINDS:
        blocked = await admin_state.blocked_reason(claims.get("sub"))
        if blocked is not None:
            return JSONResponse(status_code=503 if blocked["code"] == "maintenance" else 403, content={"detail": blocked})
    context = {"calls": {}, "meta": {}, "error": None}
    token = _current.set(context)
    started = time.monotonic()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception as exc:
        context["error"] = context["error"] or f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        _current.reset(token)
        elapsed = int((time.monotonic() - started) * 1000)
        row = {
            "kind": kind,
            "status": "completed" if status_code < 400 else "rejected" if status_code in REJECTED_STATUSES else "failed",
            "http_status": status_code,
            "user_id": claims.get("sub"),
            "email": claims.get("email"),
            "error": context["error"] if status_code >= 400 else None,
            "duration_ms": elapsed,
            "image_calls": context["calls"].get("gemini_image", 0),
            "vertex_calls": context["calls"].get("vertex", 0),
            "cost_inr": estimated_cost(context["calls"], settings),
            "meta": context["meta"],
        }
        task = asyncio.create_task(_save(app, row))
        _pending.add(task)
        task.add_done_callback(_pending.discard)


_pending: set[asyncio.Task] = set()
_missing_logged = False


async def _save(app, row: dict) -> None:
    global _missing_logged
    service = getattr(app.state, "tryon", None)
    if service is None or not service.settings.supabase_url:
        return
    try:
        response = await service.rest("POST", "activity_events", json_body=row, prefer="return=minimal")
        if response.status_code == 404 and not _missing_logged:
            log.warning("Activity is not logged: run supabase/schema.sql to create activity_events")
            _missing_logged = True
        elif response.status_code >= 400 and response.status_code != 404:
            log.warning("Could not log activity: %s %s", response.status_code, response.text[:200])
    except Exception:
        log.exception("Could not log activity")
