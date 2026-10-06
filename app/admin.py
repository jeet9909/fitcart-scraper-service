"""Admin dashboard API (/v1/admin/*) and the small public endpoints it controls (/v1/site, /v1/support).

Admins sign in at /admin with their MyDripCheck email and password; only emails listed in ADMIN_EMAILS
get an admin session. The ADMIN_API_TOKEN header keeps working for scripts. All figures come from
Supabase: Auth users, look_grants (plans, payments, bonus looks), try_on_gallery, wardrobe_items and the
admin tables in supabase/schema.sql (activity_events, support_tickets, app_settings, admin_audit).
"""

import asyncio
import hmac
import logging
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app import email_auth
from app.activity import GENERATION_KINDS
from app.anonymous_auth import verify_session_token
from app.billing import PLANS
from app.config import Settings
from app.tryon import TryOnService

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
ADMIN_TABLES = ("activity_events", "support_tickets", "app_settings", "admin_audit")
SETTINGS_TTL_SECONDS = 30
USERS_TTL_SECONDS = 60
BAN_FOREVER = "876000h"  # 100 years: Supabase's way of saying "suspended"
DEFAULT_SETTINGS: dict[str, Any] = {
    "maintenance": False,
    "maintenance_message": "MyDripCheck is getting an upgrade. New looks are paused for a few minutes.",
    "announcement": "",
    "support_email": "",
    "suspended": [],
}


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _store_of(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    known = {"myntra.com": "Myntra", "amazon.in": "Amazon", "amzn.in": "Amazon", "amazon.com": "Amazon", "ajio.com": "AJIO",
             "flipkart.com": "Flipkart", "fkrt.it": "Flipkart", "meesho.com": "Meesho", "nike.com": "Nike", "nykaafashion.com": "Nykaa Fashion",
             "tatacliq.com": "Tata CLiQ", "hm.com": "H&M", "zara.com": "Zara", "snitch.co.in": "Snitch", "souledstore.com": "The Souled Store"}
    for domain, name in known.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or None


class AdminState:
    """Dashboard settings that change how the public app behaves (maintenance, suspended accounts), cached briefly."""

    def __init__(self, service: TryOnService) -> None:
        self.service = service
        self._settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        self.table_missing = False
        self._users: tuple[float, list[dict]] | None = None

    async def settings(self, fresh: bool = False) -> dict[str, Any]:
        if self.service.settings.supabase_url and (fresh or time.monotonic() - self._loaded_at > SETTINGS_TTL_SECONDS):
            async with self._lock:
                if fresh or time.monotonic() - self._loaded_at > SETTINGS_TTL_SECONDS:
                    await self._load()
        current = dict(self._settings)
        if self.service.settings.maintenance_mode:  # MAINTENANCE_MODE in Render wins over the dashboard switch
            current["maintenance"] = True
        return current

    async def _load(self) -> None:
        try:
            response = await self.service.rest("GET", "app_settings", params={"select": "key,value"})
        except Exception:
            log.warning("Could not load app settings", exc_info=True)
            self._loaded_at = time.monotonic()
            return
        self._loaded_at = time.monotonic()
        if response.status_code == 404:
            self.table_missing = True
            return
        if response.status_code >= 400:
            return
        self.table_missing = False
        loaded = {row["key"]: row["value"] for row in response.json() if row.get("key") in DEFAULT_SETTINGS}
        self._settings = {**DEFAULT_SETTINGS, **loaded}

    async def save(self, values: dict[str, Any]) -> dict[str, Any]:
        self._settings = {**self._settings, **values}  # takes effect on this server at once, whatever the database says
        if not self.service.settings.supabase_url:
            return await self.settings()
        rows = [{"key": key, "value": value, "updated_at": _now().isoformat()} for key, value in values.items()]
        response = await self.service.rest("POST", "app_settings", params={"on_conflict": "key"}, json_body=rows,
                                           prefer="resolution=merge-duplicates,return=minimal")
        if response.status_code >= 400:
            # Only when Supabase's table and the backup storage both fail: the switch still works on this server.
            log.warning("Settings kept in memory only: %s %s", response.status_code, response.text[:200])
            return await self.settings()
        return await self.settings(fresh=True)

    async def blocked_reason(self, user_id: str | None) -> dict | None:
        current = await self.settings()
        if current.get("maintenance"):
            return {"code": "maintenance", "message": current.get("maintenance_message") or DEFAULT_SETTINGS["maintenance_message"]}
        if user_id and user_id in set(current.get("suspended") or []):
            return {"code": "account_suspended", "message": "This account is suspended. Contact support if you think this is a mistake."}
        return None

    async def auth_users(self, fresh: bool = False) -> list[dict]:
        """All Supabase Auth accounts (cached for a minute)."""
        if not fresh and self._users and time.monotonic() - self._users[0] < USERS_TTL_SECONDS:
            return self._users[1]
        settings = self.service.settings
        users: list[dict] = []
        for page in range(1, 21):
            response = await email_auth._auth_request(settings, "GET", "/admin/users", params={"page": page, "per_page": 1000})
            if response.status_code >= 400:
                raise HTTPException(status_code=502, detail="Could not read accounts from Supabase Auth.")
            batch = response.json().get("users", [])
            users += batch
            if len(batch) < 1000:
                break
        self._users = (time.monotonic(), users)
        return users


bearer = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/v1/admin", tags=["admin"])
public = APIRouter(tags=["site"])


def get_service(request: Request) -> TryOnService:
    return request.app.state.tryon


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_state(request: Request) -> AdminState:
    return request.app.state.admin_state


def require_admin_access(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    x_admin_token: str | None = Header(None),
    settings: Settings = Depends(get_settings),
) -> str:
    """The admin's email, from an admin session token (or 'api-token' for the ADMIN_API_TOKEN header)."""
    expected = settings.admin_api_token.get_secret_value()
    if x_admin_token and expected and hmac.compare_digest(x_admin_token, expected):
        return "api-token"
    if credentials and credentials.scheme.lower() == "bearer":
        try:
            claims = jwt.decode(credentials.credentials, settings.anonymous_token_secret.get_secret_value(), algorithms=["HS256"])
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail="Your admin session has ended. Sign in again.") from exc
        if claims.get("type") == "admin" and settings.is_admin(claims.get("email")):
            return str(claims["email"])
    raise HTTPException(status_code=401, detail="Admin sign-in required.")


class AdminLogin(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class AddLooks(BaseModel):
    looks: int = Field(ge=1, le=1000)
    days: int = Field(default=30, ge=1, le=365)
    reason: str = Field(default="", max_length=200)


class SetSuspended(BaseModel):
    suspended: bool


class TicketUpdate(BaseModel):
    status: Literal["open", "resolved"] | None = None
    priority: Literal["normal", "high"] | None = None
    note: str | None = Field(default=None, max_length=4000)


class SettingsUpdate(BaseModel):
    maintenance: bool | None = None
    maintenance_message: str | None = Field(default=None, max_length=300)
    announcement: str | None = Field(default=None, max_length=300)
    support_email: str | None = Field(default=None, max_length=254)


class NewTicket(BaseModel):
    email: str | None = Field(default=None, max_length=254)
    subject: str = Field(min_length=3, max_length=150)
    message: str = Field(min_length=5, max_length=4000)


async def _rows(service: TryOnService, table: str, params: dict[str, str], missing: set[str] | None = None) -> list[dict]:
    try:
        response = await service.rest("GET", table, params=params)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not reach Supabase.") from exc
    if response.status_code == 404:
        if missing is not None:
            missing.add(table)
        return []
    if response.status_code >= 400:
        log.warning("Admin query on %s failed: %s %s", table, response.status_code, response.text[:200])
        raise HTTPException(status_code=502, detail=f"Could not read {table} from Supabase.")
    return response.json()


async def _audit(service: TryOnService, actor: str, action: str, target: str | None = None, detail: dict | None = None) -> None:
    try:
        await service.rest("POST", "admin_audit", json_body={"admin_email": actor, "action": action, "target": target, "detail": detail or {}},
                           prefer="return=minimal")
    except Exception:
        log.warning("Could not write the admin audit log", exc_info=True)


def _backup_status(service: TryOnService) -> dict:
    backup = getattr(service, "backup", None)
    return backup.status() if backup else {"tables_on_backup": {}, "storage_error": None}


def _require_supabase(settings: Settings) -> None:
    if not settings.supabase_url or not settings.supabase_service_role_key.get_secret_value():
        raise HTTPException(status_code=503, detail="Supabase is not configured on the server.")


# ---------------------------------------------------------------- session


@router.post("/session")
async def admin_session(body: AdminLogin, settings: Settings = Depends(get_settings)) -> dict:
    """Sign in to the admin dashboard with a MyDripCheck account whose email is in ADMIN_EMAILS."""
    email = email_auth.normalize_email(body.email)
    if not settings.admin_emails_csv.strip():
        raise HTTPException(status_code=503, detail="No admins yet: add your email to ADMIN_EMAILS in Render, then deploy.")
    user_id, email = await email_auth.log_in(email, body.password, settings)
    if not settings.is_admin(email):
        raise HTTPException(status_code=403, detail="This account is not an admin.")
    expires = _now() + timedelta(hours=settings.admin_session_hours)
    token = jwt.encode({"sub": user_id, "email": email, "type": "admin", "exp": expires},
                       settings.anonymous_token_secret.get_secret_value(), algorithm="HS256")
    return {"access_token": token, "email": email, "expires_at": expires.isoformat()}


@router.get("/me")
async def admin_me(actor: str = Depends(require_admin_access)) -> dict:
    return {"email": actor}


# ---------------------------------------------------------------- shared loaders


async def _events(service: TryOnService, since: datetime, missing: set[str], extra: dict[str, str] | None = None, limit: int = 20000) -> list[dict]:
    params = {"select": "*", "created_at": f"gte.{since.isoformat()}", "order": "created_at.desc", "limit": str(limit), **(extra or {})}
    return await _rows(service, "activity_events", params, missing)


async def _purchases(service: TryOnService, missing: set[str]) -> list[dict]:
    """One row per Razorpay payment, rebuilt from look_grants (a yearly plan has 12 monthly grants)."""
    rows = await _rows(service, "look_grants", {"select": "user_id,kind,looks,payment_ref,created_at", "payment_ref": "not.is.null",
                                                 "kind": "in.(pass,plus,pro)", "order": "created_at.desc", "limit": "50000"}, missing)
    grouped: dict[str, dict] = {}
    for row in rows:
        ref = str(row.get("payment_ref") or "")
        if not ref or ref.startswith("admin:"):
            continue
        base = ref.split(":")[0]
        entry = grouped.setdefault(base, {"ref": base, "user_id": row["user_id"], "kind": row["kind"], "grants": 0, "created_at": row["created_at"]})
        entry["grants"] += 1
        if str(row["created_at"]) < str(entry["created_at"]):
            entry["created_at"] = row["created_at"]
    purchases = []
    for entry in grouped.values():
        plan = PLANS.get(entry["kind"])
        if plan is None:
            continue
        if entry["kind"] == "pass":
            billing, paise = "once", plan.once
        elif entry["grants"] > 1:
            billing, paise = "yearly", plan.yearly
        else:
            billing, paise = "monthly", plan.monthly
        purchases.append({**entry, "billing": billing, "amount_inr": paise / 100})
    purchases.sort(key=lambda p: str(p["created_at"]), reverse=True)
    return purchases


def _in_window(value: Any, start: datetime, end: datetime | None = None) -> bool:
    moment = _parse_time(value)
    return bool(moment) and moment >= start and (end is None or moment < end)


def _pct_change(current: float, previous: float) -> float | None:
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def _event_view(row: dict, emails: dict[str, str] | None = None) -> dict:
    meta = row.get("meta") or {}
    return {
        "id": row.get("id"), "kind": row.get("kind"), "status": row.get("status"), "http_status": row.get("http_status"),
        "user_id": row.get("user_id"), "email": row.get("email") or (emails or {}).get(str(row.get("user_id")), None),
        "error": row.get("error"), "duration_ms": row.get("duration_ms"), "cost_inr": float(row.get("cost_inr") or 0),
        "image_calls": row.get("image_calls") or 0, "vertex_calls": row.get("vertex_calls") or 0,
        "meta": meta, "created_at": row.get("created_at"),
    }


def _generation_stats(events: list[dict]) -> dict:
    generation = [e for e in events if e.get("kind") in GENERATION_KINDS]
    completed = [e for e in generation if e.get("status") == "completed"]
    failed = [e for e in generation if e.get("status") == "failed"]
    looks = [e for e in completed if e.get("kind") in ("look", "outfit")]
    durations = [e.get("duration_ms") or 0 for e in looks]
    cost = sum(float(e.get("cost_inr") or 0) for e in events)
    return {
        "looks": len(looks),
        "completed": len(completed),
        "failed": len(failed),
        "rejected": sum(e.get("status") == "rejected" for e in generation),
        "success_rate": round(len(completed) / (len(completed) + len(failed)) * 100, 1) if completed or failed else None,
        "avg_seconds": round(sum(durations) / len(durations) / 1000, 1) if durations else None,
        "cost_inr": round(cost, 2),
        "avg_cost_inr": round(sum(float(e.get("cost_inr") or 0) for e in looks) / len(looks), 2) if looks else None,
        "active_users": len({e.get("user_id") for e in generation if e.get("user_id")}),
    }


def _daily(events: list[dict], days: int) -> list[dict]:
    today = _now().astimezone(IST).date()
    buckets = {today - timedelta(days=offset): {"completed": 0, "failed": 0} for offset in range(days)}
    for event in events:
        if event.get("kind") not in GENERATION_KINDS:
            continue
        moment = _parse_time(event.get("created_at"))
        if not moment:
            continue
        day = moment.astimezone(IST).date()
        if day in buckets and event.get("status") in ("completed", "failed"):
            buckets[day][event["status"]] += 1
    return [{"date": day.isoformat(), **counts} for day, counts in sorted(buckets.items())]


def _store_health(events: list[dict]) -> list[dict]:
    stats: dict[str, Counter] = defaultdict(Counter)
    for event in events:
        if event.get("kind") != "scrape":
            continue
        store = (event.get("meta") or {}).get("store") or "Other"
        stats[store]["total"] += 1
        stats[store][event.get("status") or "failed"] += 1
    out = []
    for store, counts in stats.items():
        tried = counts["completed"] + counts["failed"]
        rate = round(counts["completed"] / tried * 100, 1) if tried else None
        out.append({"store": store, "attempts": counts["total"], "completed": counts["completed"], "failed": counts["failed"], "success_rate": rate,
                    "status": "Healthy" if rate is None or rate >= 90 else "Degraded" if rate >= 60 else "Offline"})
    return sorted(out, key=lambda s: -s["attempts"])


# ---------------------------------------------------------------- overview


@router.get("/overview")
async def overview(days: int = 7, actor: str = Depends(require_admin_access), service: TryOnService = Depends(get_service),
                   settings: Settings = Depends(get_settings), state: AdminState = Depends(get_state)) -> dict:
    _require_supabase(settings)
    days = 30 if days >= 30 else 7
    now = _now()
    start, prev_start = now - timedelta(days=days), now - timedelta(days=2 * days)
    missing: set[str] = set()
    events_all, purchases, users, tickets = await asyncio.gather(
        _events(service, prev_start, missing),
        _purchases(service, missing),
        state.auth_users(),
        _rows(service, "support_tickets", {"select": "id", "status": "eq.open"}, missing),
    )
    current = [e for e in events_all if _in_window(e.get("created_at"), start)]
    previous = [e for e in events_all if _in_window(e.get("created_at"), prev_start, start)]
    revenue_now = sum(p["amount_inr"] for p in purchases if _in_window(p["created_at"], start))
    revenue_prev = sum(p["amount_inr"] for p in purchases if _in_window(p["created_at"], prev_start, start))
    stats_now, stats_prev = _generation_stats(current), _generation_stats(previous)
    signups_now = sum(_in_window(u.get("created_at"), start) for u in users)
    signups_prev = sum(_in_window(u.get("created_at"), prev_start, start) for u in users)
    emails = {u["id"]: u.get("email") for u in users}
    stores = _store_health(current)
    alerts = [{"level": "warn", "title": f"{s['store']} product links are failing",
               "message": f"{s['failed']} of {s['completed'] + s['failed']} imports failed in the last {days} days.", "page": "integrations"}
              for s in stores if s["status"] != "Healthy" and s["completed"] + s["failed"] >= 3]
    if stats_now["success_rate"] is not None and stats_now["success_rate"] < 90 and stats_now["completed"] + stats_now["failed"] >= 5:
        alerts.append({"level": "bad", "title": "Try-ons are failing more than usual",
                       "message": f"Success rate is {stats_now['success_rate']}% over the last {days} days.", "page": "tryons"})
    current_settings = await state.settings()
    if current_settings.get("maintenance"):
        alerts.insert(0, {"level": "warn", "title": "Maintenance mode is on", "message": "New looks are paused for every user.", "page": "settings"})
    if missing & set(ADMIN_TABLES):
        alerts.insert(0, {"level": "warn", "title": "Finish the database setup",
                          "message": "Run supabase/schema.sql in the Supabase SQL editor to turn on activity, support and settings.", "page": "settings"})
    return {
        "days": days,
        "revenue": {"value": revenue_now, "change": _pct_change(revenue_now, revenue_prev)},
        "active_users": {"value": stats_now["active_users"], "change": _pct_change(stats_now["active_users"], stats_prev["active_users"])},
        "looks": {"value": stats_now["looks"], "change": _pct_change(stats_now["looks"], stats_prev["looks"])},
        "success_rate": {"value": stats_now["success_rate"], "previous": stats_prev["success_rate"]},
        "signups": {"value": signups_now, "change": _pct_change(signups_now, signups_prev), "total": len(users)},
        "avg_seconds": stats_now["avg_seconds"], "avg_cost_inr": stats_now["avg_cost_inr"], "cost_inr": stats_now["cost_inr"],
        "failed": stats_now["failed"],
        "series": _daily(current, days),
        "latest": [_event_view(e, emails) for e in current if e.get("kind") in GENERATION_KINDS][:6],
        "stores": stores,
        "open_tickets": len(tickets),
        "alerts": alerts,
        "missing_tables": sorted(missing & set(ADMIN_TABLES)),
        "test_mode": not settings.razorpay_key_id.strip().startswith("rzp_live_"),
    }


# ---------------------------------------------------------------- users


def _plan_from(kinds: set[str]) -> str:
    return next((kind for kind in ("pro", "plus", "pass") if kind in kinds), "free")


@router.get("/users")
async def list_users(actor: str = Depends(require_admin_access), service: TryOnService = Depends(get_service),
                     settings: Settings = Depends(get_settings), state: AdminState = Depends(get_state)) -> dict:
    _require_supabase(settings)
    now = _now()
    users, grants, looks, current_settings = await asyncio.gather(
        state.auth_users(fresh=True),
        _rows(service, "look_grants", {"select": "user_id,kind,looks,used,period", "starts_at": f"lte.{now.isoformat()}",
                                       "expires_at": f"gt.{now.isoformat()}", "limit": "100000"}),
        _rows(service, "try_on_gallery", {"select": "anonymous_user_id", "limit": "100000"}),
        state.settings(),
    )
    by_user: dict[str, list[dict]] = defaultdict(list)
    for grant in grants:
        by_user[str(grant["user_id"])].append(grant)
    look_counts = Counter(str(row["anonymous_user_id"]) for row in looks)
    suspended = set(current_settings.get("suspended") or [])
    month = now.astimezone(IST).strftime("%Y-%m")
    out = []
    for user in users:
        user_id, email = str(user["id"]), (user.get("email") or "").lower()
        mine = by_user.get(user_id, [])
        remaining = sum(max(0, (g.get("looks") or 0) - (g.get("used") or 0)) for g in mine)
        if not any(g["kind"] == "free" and g.get("period") == month for g in mine):
            remaining += settings.free_looks_per_month  # the free grant is created on first use each month
        banned = _parse_time(user.get("banned_until"))
        out.append({
            "id": user_id, "email": email, "created_at": user.get("created_at"), "last_sign_in_at": user.get("last_sign_in_at"),
            "plan": "unlimited" if settings.is_unlimited(email) else _plan_from({g["kind"] for g in mine}),
            "looks_left": None if settings.is_unlimited(email) else remaining,
            "looks_made": look_counts.get(user_id, 0),
            "status": "suspended" if user_id in suspended or (banned and banned > now) else "active",
            "admin": settings.is_admin(email),
        })
    out.sort(key=lambda u: str(u.get("created_at") or ""), reverse=True)
    week = now - timedelta(days=7)
    return {
        "users": out,
        "total": len(out),
        "paying": sum(u["plan"] in ("pass", "plus", "pro") for u in out),
        "new_this_week": sum(_in_window(u["created_at"], week) for u in out),
        "suspended": sum(u["status"] == "suspended" for u in out),
        "free_looks_per_month": settings.free_looks_per_month,
    }


@router.get("/users/{user_id}")
async def user_detail(user_id: UUID, actor: str = Depends(require_admin_access), service: TryOnService = Depends(get_service),
                      settings: Settings = Depends(get_settings), state: AdminState = Depends(get_state)) -> dict:
    _require_supabase(settings)
    uid = str(user_id)
    user = next((u for u in await state.auth_users() if str(u["id"]) == uid), None)
    if user is None:
        user = next((u for u in await state.auth_users(fresh=True) if str(u["id"]) == uid), None)
    if user is None:
        raise HTTPException(status_code=404, detail="No account with that id.")
    grants, looks, events, purchases = await asyncio.gather(
        _rows(service, "look_grants", {"select": "id,kind,looks,used,period,starts_at,expires_at,payment_ref,created_at",
                                       "user_id": f"eq.{uid}", "order": "created_at.desc", "limit": "60"}),
        _rows(service, "try_on_gallery", {"select": "id,category,items,product_url,created_at", "anonymous_user_id": f"eq.{uid}",
                                          "order": "created_at.desc", "limit": "20"}),
        _rows(service, "activity_events", {"select": "*", "user_id": f"eq.{uid}", "order": "created_at.desc", "limit": "30"}),
        _purchases(service, set()),
    )
    now = _now()
    return {
        "id": uid, "email": user.get("email"), "created_at": user.get("created_at"), "last_sign_in_at": user.get("last_sign_in_at"),
        "banned_until": user.get("banned_until"),
        "suspended": uid in set((await state.settings()).get("suspended") or []),
        "unlimited": settings.is_unlimited(user.get("email")),
        "grants": [{**g, "remaining": max(0, (g.get("looks") or 0) - (g.get("used") or 0)),
                    "active": (_parse_time(g["starts_at"]) or now) <= now < (_parse_time(g["expires_at"]) or now)}
                   for g in grants],
        "looks": looks,
        "events": [_event_view(e) for e in events],
        "purchases": [p for p in purchases if str(p["user_id"]) == uid],
    }


@router.post("/users/{user_id}/looks")
async def add_looks(user_id: UUID, body: AddLooks, actor: str = Depends(require_admin_access), service: TryOnService = Depends(get_service),
                    settings: Settings = Depends(get_settings)) -> dict:
    _require_supabase(settings)
    now = _now()
    row = {"user_id": str(user_id), "kind": "bonus", "looks": body.looks, "used": 0, "starts_at": now.isoformat(),
           "expires_at": (now + timedelta(days=body.days)).isoformat(), "payment_ref": f"admin:{uuid4()}"}
    response = await service.rest("POST", "look_grants", json_body=row, prefer="return=minimal")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Could not add looks: {response.text[:200]}")
    await _audit(service, actor, "add_looks", str(user_id), {"looks": body.looks, "days": body.days, "reason": body.reason})
    return {"added": body.looks, "expires_at": row["expires_at"]}


@router.post("/users/{user_id}/status")
async def set_user_status(user_id: UUID, body: SetSuspended, actor: str = Depends(require_admin_access), service: TryOnService = Depends(get_service),
                          settings: Settings = Depends(get_settings), state: AdminState = Depends(get_state)) -> dict:
    _require_supabase(settings)
    uid = str(user_id)
    user = next((u for u in await state.auth_users(fresh=True) if str(u["id"]) == uid), None)
    if user is None:
        raise HTTPException(status_code=404, detail="No account with that id.")
    if body.suspended and settings.is_admin(user.get("email")):
        raise HTTPException(status_code=400, detail="Admins cannot be suspended. Remove the email from ADMIN_EMAILS first.")
    response = await email_auth._auth_request(settings, "PUT", f"/admin/users/{uid}", json={"ban_duration": BAN_FOREVER if body.suspended else "none"})
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Supabase Auth refused the change.")
    suspended = set((await state.settings(fresh=True)).get("suspended") or [])
    suspended = suspended | {uid} if body.suspended else suspended - {uid}
    if not state.table_missing:
        await state.save({"suspended": sorted(suspended)})
    await state.auth_users(fresh=True)
    await _audit(service, actor, "suspend" if body.suspended else "restore", uid, {"email": user.get("email")})
    return {"id": uid, "status": "suspended" if body.suspended else "active"}


# ---------------------------------------------------------------- activity


@router.get("/activity")
async def activity(days: int = 30, kind: str | None = None, status_filter: str | None = None, limit: int = 300,
                   actor: str = Depends(require_admin_access), service: TryOnService = Depends(get_service),
                   settings: Settings = Depends(get_settings), state: AdminState = Depends(get_state)) -> dict:
    _require_supabase(settings)
    days = max(1, min(days, 90))
    extra: dict[str, str] = {}
    if kind in ("look", "outfit", "spin", "pose", "scrape"):
        extra["kind"] = f"eq.{kind}"
    if status_filter in ("completed", "failed", "rejected"):
        extra["status"] = f"eq.{status_filter}"
    missing: set[str] = set()
    since = _now() - timedelta(days=days)
    events, all_events, users = await asyncio.gather(
        _events(service, since, missing, extra, limit=max(1, min(limit, 1000))),
        _events(service, since, set()),
        state.auth_users(),
    )
    emails = {u["id"]: u.get("email") for u in users}
    stats = _generation_stats(all_events)
    return {"events": [_event_view(e, emails) for e in events], "stats": stats, "days": days,
            "setup_needed": "activity_events" in missing}


# ---------------------------------------------------------------- products


@router.get("/products")
async def products(actor: str = Depends(require_admin_access), service: TryOnService = Depends(get_service),
                   settings: Settings = Depends(get_settings)) -> dict:
    """Products people try on and save, grouped across every look and wardrobe."""
    _require_supabase(settings)
    looks, saved = await asyncio.gather(
        _rows(service, "try_on_gallery", {"select": "category,items,product_url,created_at", "order": "created_at.desc", "limit": "20000"}),
        _rows(service, "wardrobe_items", {"select": "name,brand,price,store,slot,product_url,created_at", "collection": "eq.store",
                                          "order": "created_at.desc", "limit": "20000"}),
    )
    catalog: dict[str, dict] = {}

    def entry(key: str, **fields: Any) -> dict:
        item = catalog.setdefault(key, {"name": None, "store": None, "price": None, "slot": None, "product_url": None,
                                        "tried": 0, "saved": 0, "last_seen": None})
        for field, value in fields.items():
            if value not in (None, "") and item.get(field) in (None, ""):
                item[field] = value
        return item

    for look in looks:
        pieces = look.get("items") or [{"name": None, "category": look.get("category"), "product_url": look.get("product_url")}]
        for piece in pieces:
            url = piece.get("product_url")
            name = piece.get("name")
            if not url and not name:
                continue
            item = entry(url or f"name:{str(name).lower()}", name=name, store=piece.get("store") or _store_of(url), price=piece.get("price"),
                         slot=piece.get("slot") or "top", product_url=url)
            item["tried"] += 1
            item["last_seen"] = max(filter(None, [item["last_seen"], look.get("created_at")]), default=None)
    for row in saved:
        url = row.get("product_url")
        item = entry(url or f"name:{str(row.get('name') or '').lower()}", name=row.get("name"), store=row.get("store") or _store_of(url),
                     price=row.get("price"), slot=row.get("slot"), product_url=url)
        item["saved"] += 1
        item["last_seen"] = max(filter(None, [item["last_seen"], row.get("created_at")]), default=None)
    items = sorted(catalog.values(), key=lambda i: (i["tried"] + i["saved"], str(i["last_seen"] or "")), reverse=True)
    stores = Counter(i["store"] or "Other" for i in items)
    return {"products": items[:300], "total": len(items), "stores": stores.most_common(12),
            "looks": len(looks), "saved": len(saved)}


# ---------------------------------------------------------------- revenue


@router.get("/revenue")
async def revenue(days: int = 30, actor: str = Depends(require_admin_access), service: TryOnService = Depends(get_service),
                  settings: Settings = Depends(get_settings), state: AdminState = Depends(get_state)) -> dict:
    _require_supabase(settings)
    days = max(1, min(days, 365))
    now = _now()
    start, prev_start = now - timedelta(days=days), now - timedelta(days=2 * days)
    missing: set[str] = set()
    purchases, events, users = await asyncio.gather(_purchases(service, missing), _events(service, start, missing), state.auth_users())
    emails = {u["id"]: u.get("email") for u in users}
    current = [p for p in purchases if _in_window(p["created_at"], start)]
    gross = sum(p["amount_inr"] for p in current)
    previous = sum(p["amount_inr"] for p in purchases if _in_window(p["created_at"], prev_start, start))
    generation_cost = round(sum(float(e.get("cost_inr") or 0) for e in events), 2)
    fixed = round(settings.monthly_fixed_costs_inr * days / 30, 2)
    gst = round(gross - gross / 1.18, 2)  # prices include 18% GST
    by_plan = []
    for key, plan in PLANS.items():
        mine = [p for p in current if p["kind"] == key]
        by_plan.append({"plan": key, "name": plan.name, "count": len(mine), "amount_inr": sum(p["amount_inr"] for p in mine)})
    plans = [{"key": "free", "name": "Free", "looks": settings.free_looks_per_month, "prices": {"monthly": 0}, "note": "Every month, after sign-in"}]
    plans += [{"key": key, "name": plan.name, "looks": plan.looks,
               "prices": {"once": plan.once / 100} if plan.once else {"monthly": plan.monthly / 100, "yearly": plan.yearly / 100},
               "note": f"{plan.looks} looks for {plan.days} days" if plan.days else f"{plan.looks} looks every month"} for key, plan in PLANS.items()]
    return {
        "days": days, "gross_inr": gross, "previous_inr": previous, "change": _pct_change(gross, previous),
        "gst_inr": gst, "net_inr": round(gross - gst, 2), "generation_cost_inr": generation_cost, "fixed_cost_inr": fixed,
        "contribution_inr": round(gross - gst - generation_cost - fixed, 2),
        "purchases": [{**p, "email": emails.get(str(p["user_id"]))} for p in current[:200]],
        "by_plan": by_plan, "plans": plans, "all_time_inr": sum(p["amount_inr"] for p in purchases),
        "test_mode": not settings.razorpay_key_id.strip().startswith("rzp_live_"),
        "costs": {"gemini_image": settings.cost_gemini_image_inr, "vertex": settings.cost_vertex_tryon_inr, "monthly_fixed": settings.monthly_fixed_costs_inr},
        "setup_needed": "activity_events" in missing,
    }


# ---------------------------------------------------------------- integrations


async def _timed(check) -> dict:
    started = time.monotonic()
    try:
        result = await check()
    except Exception as exc:  # a broken check reports itself instead of failing the page
        result = {"status": "Offline", "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result


@router.get("/integrations")
async def integrations(actor: str = Depends(require_admin_access), service: TryOnService = Depends(get_service),
                       settings: Settings = Depends(get_settings)) -> dict:
    async def gemini() -> dict:
        key = settings.gemini_api_key.get_secret_value()
        if not key:
            return {"status": "Not set up", "detail": "GEMINI_API_KEY is missing, so looks cannot be generated."}
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(service._model_url(), headers={"x-goog-api-key": key})
        if response.status_code == 200:
            return {"status": "Healthy", "detail": f"Key works and {settings.gemini_image_model} is available."}
        return {"status": "Offline", "detail": f"Google answered {response.status_code}: {service._error_message(response)[:160]}"}

    async def database() -> dict:
        if not settings.supabase_url:
            return {"status": "Not set up", "detail": "SUPABASE_URL is missing."}
        response = await service.rest("GET", "try_on_gallery", params={"select": "id", "limit": "1"})
        if response.status_code >= 400:
            return {"status": "Offline", "detail": f"Supabase answered {response.status_code}."}
        for table in ADMIN_TABLES:  # each read records whether Supabase's table API answered
            await service.rest("GET", table, params={"select": "*", "limit": "1"})
        backup = _backup_status(service)
        if backup["tables_on_backup"]:
            names = ", ".join(sorted(backup["tables_on_backup"]))
            where = "backup storage" if not backup["storage_error"] else "server memory (backup storage failed too)"
            return {"status": "Degraded", "detail": f"Database works. {names} run on {where}: every admin feature still works. "
                    "To move them back, run supabase/schema.sql in the Supabase SQL editor."}
        return {"status": "Healthy", "detail": "Database, gallery and admin tables are reachable."}

    async def auth() -> dict:
        if not settings.supabase_url:
            return {"status": "Not set up", "detail": "SUPABASE_URL is missing."}
        response = await email_auth._auth_request(settings, "GET", "/health")
        return {"status": "Healthy" if response.status_code == 200 else "Offline", "detail": "Email and password sign-in through Supabase Auth."
                if response.status_code == 200 else f"Supabase Auth answered {response.status_code}."}

    async def razorpay() -> dict:
        key_id, secret = settings.razorpay_key_id.strip(), settings.razorpay_key_secret.get_secret_value()
        if not key_id or not secret:
            return {"status": "Not set up", "detail": "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are missing, so nobody can pay."}
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("https://api.razorpay.com/v1/payments", params={"count": 1}, auth=(key_id, secret))
        mode = "Live mode" if key_id.startswith("rzp_live_") else "Test mode: no real money moves"
        webhook = "" if settings.razorpay_webhook_secret.get_secret_value() else " The webhook secret is not set."
        if response.status_code == 200:
            return {"status": "Healthy" if not webhook else "Degraded", "detail": f"{mode}.{webhook}"}
        return {"status": "Offline", "detail": f"Razorpay refused the keys ({response.status_code}). {mode}."}

    async def brightdata() -> dict:
        if not settings.brightdata_api_token.get_secret_value():
            return {"status": "Not set up", "detail": "BRIGHTDATA_API_TOKEN is missing, so store links cannot be imported."}
        return {"status": "Healthy", "detail": f"Token set (zone {settings.brightdata_zone}). Store success rates are below."}

    async def vertex() -> dict:
        if not settings.vertex_tryon_enabled:
            return {"status": "Off", "detail": "Turned off with VERTEX_TRYON_ENABLED. 'My pose' uses Gemini."}
        if not service.vertex.configured:
            return {"status": "Not set up", "detail": "GOOGLE_SERVICE_ACCOUNT_JSON is missing. 'My pose' uses Gemini until it is added."}
        return {"status": "Healthy", "detail": f"Vertex Virtual Try-On ({settings.vertex_tryon_model}) handles 'My pose' looks."}

    names = [("gemini", "Image generation", "Google Gemini"), ("database", "Database & gallery", "Supabase"),
             ("auth", "Sign-in", "Supabase Auth"), ("razorpay", "Payments", "Razorpay"),
             ("brightdata", "Product imports", "Bright Data"), ("vertex", "My-pose try-on", "Google Vertex AI")]
    checks = await asyncio.gather(*(_timed(fn) for fn in (gemini, database, auth, razorpay, brightdata, vertex)))
    missing: set[str] = set()
    events = await _events(service, _now() - timedelta(days=7), missing, {"kind": "eq.scrape"}) if settings.supabase_url else []
    usage = service.usage
    return {
        "services": [{"key": key, "name": name, "provider": provider, **check} for (key, name, provider), check in zip(names, checks)],
        "stores": _store_health(events),
        "gemini_since_restart": {"since": usage.since.isoformat(), "requests": usage.requests, "succeeded": usage.succeeded,
                                 "failed": usage.failed, "last_error": usage.last_error},
        "checked_at": _now().isoformat(),
    }


# ---------------------------------------------------------------- support


@router.get("/support")
async def list_tickets(status_filter: str | None = None, actor: str = Depends(require_admin_access), service: TryOnService = Depends(get_service),
                       settings: Settings = Depends(get_settings)) -> dict:
    _require_supabase(settings)
    params = {"select": "*", "order": "created_at.desc", "limit": "500"}
    if status_filter in ("open", "resolved"):
        params["status"] = f"eq.{status_filter}"
    missing: set[str] = set()
    tickets = await _rows(service, "support_tickets", params, missing)
    return {"tickets": tickets, "setup_needed": "support_tickets" in missing}


@router.patch("/support/{ticket_id}")
async def update_ticket(ticket_id: UUID, body: TicketUpdate, actor: str = Depends(require_admin_access), service: TryOnService = Depends(get_service),
                        settings: Settings = Depends(get_settings)) -> dict:
    _require_supabase(settings)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    changes["updated_at"] = _now().isoformat()
    response = await service.rest("PATCH", "support_tickets", params={"id": f"eq.{ticket_id}"}, json_body=changes, prefer="return=representation")
    if response.status_code >= 400 or not response.json():
        raise HTTPException(status_code=404 if response.status_code < 400 else 502, detail="Could not update the ticket.")
    await _audit(service, actor, "ticket", str(ticket_id), {k: v for k, v in changes.items() if k != "note"})
    return response.json()[0]


# ---------------------------------------------------------------- settings and audit


@router.get("/settings")
async def get_admin_settings(actor: str = Depends(require_admin_access), service: TryOnService = Depends(get_service),
                             settings: Settings = Depends(get_settings), state: AdminState = Depends(get_state)) -> dict:
    current = await state.settings(fresh=True)
    missing: set[str] = set()
    audit = await _rows(service, "admin_audit", {"select": "*", "order": "created_at.desc", "limit": "40"}, missing) if settings.supabase_url else []
    return {
        "settings": {k: v for k, v in current.items() if k != "suspended"},
        "suspended_count": len(current.get("suspended") or []),
        "server": {
            "free_looks_per_month": settings.free_looks_per_month, "look_limits_enabled": settings.look_limits_enabled,
            "unlimited_emails": [e.strip() for e in settings.unlimited_emails_csv.split(",") if e.strip()],
            "admin_emails": [e.strip() for e in settings.admin_emails_csv.split(",") if e.strip()],
            "image_model": settings.gemini_image_model, "face_check_target": settings.face_match_target,
            "razorpay_autopay": settings.razorpay_autopay,
        },
        "audit": audit,
        "setup_needed": state.table_missing or bool(missing),
        "backup": _backup_status(service),
        "maintenance_forced": settings.maintenance_mode,
    }


@router.put("/settings")
async def put_admin_settings(body: SettingsUpdate, actor: str = Depends(require_admin_access), service: TryOnService = Depends(get_service),
                             state: AdminState = Depends(get_state)) -> dict:
    changes = body.model_dump(exclude_none=True)
    if "support_email" in changes and changes["support_email"]:
        changes["support_email"] = email_auth.normalize_email(changes["support_email"])
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    saved = await state.save(changes)
    await _audit(service, actor, "settings", None, changes)
    return {k: v for k, v in saved.items() if k != "suspended"}


# ---------------------------------------------------------------- public: site status and support form


@public.get("/v1/site")
async def site_status(state: AdminState = Depends(get_state)) -> dict:
    """Banner and maintenance state for the storefront."""
    current = await state.settings()
    return {"maintenance": bool(current.get("maintenance")), "maintenance_message": current.get("maintenance_message") if current.get("maintenance") else None,
            "announcement": current.get("announcement") or None, "support_email": current.get("support_email") or None}


_ticket_times: dict[str, list[float]] = {}


@public.post("/v1/support", status_code=201)
async def create_ticket(body: NewTicket, request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
                        service: TryOnService = Depends(get_service), settings: Settings = Depends(get_settings)) -> dict:
    """Send a message to the MyDripCheck team. Signed-in users are linked to their account."""
    _require_supabase(settings)
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    recent = [t for t in _ticket_times.get(client, []) if now - t < 3600]
    if len(recent) >= 5:
        raise HTTPException(status_code=429, detail="You have sent several messages already. Please wait a while before sending another.")
    _ticket_times[client] = [*recent, now]
    claims: dict = {}
    if credentials and credentials.scheme.lower() == "bearer":
        try:
            claims = verify_session_token(credentials.credentials, settings)
        except HTTPException:
            claims = {}
    email = claims.get("email") or (email_auth.normalize_email(body.email) if body.email else None)
    if not email:
        raise HTTPException(status_code=422, detail="Add your email so we can reply.")
    text = (body.subject + " " + body.message).lower()
    priority = "high" if any(word in text for word in ("payment", "paid", "refund", "charged", "money", "looks missing")) else "normal"
    row = {"user_id": claims.get("sub") if claims.get("email") else None, "email": email, "subject": body.subject.strip(),
           "message": body.message.strip(), "status": "open", "priority": priority}
    response = await service.rest("POST", "support_tickets", json_body=row, prefer="return=representation")
    if response.status_code == 404:
        raise HTTPException(status_code=503, detail="Support messages are not set up yet.")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Could not send your message. Please try again.")
    return {"id": response.json()[0]["id"], "status": "open"}
