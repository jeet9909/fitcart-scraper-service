"""Email + password accounts in Supabase Auth, exchanged for a FitCart session token.

Accounts are created through Supabase's admin API as already confirmed, so no confirmation email is
sent. Passwords are checked by Supabase (grant_type=password); FitCart never stores them. Repeated
wrong passwords for one email are slowed down here as well.
"""

import logging
import re
import time

import httpx
from fastapi import HTTPException, status

from app.config import Settings

log = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SUPABASE_AUTH_TIMEOUT_SECONDS = 15
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 72  # bcrypt, which Supabase uses, ignores anything longer
MAX_FAILED_LOGINS = 8
FAILED_LOGIN_WINDOW_SECONDS = 15 * 60
_failed_logins: dict[str, list[float]] = {}  # email -> times of recent wrong passwords (per process)


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_PATTERN.match(email) or len(email) > 254:
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    return email


def check_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=422, detail=f"Use at least {MIN_PASSWORD_LENGTH} characters for your password.")
    if len(password.encode()) > MAX_PASSWORD_LENGTH:
        raise HTTPException(status_code=422, detail=f"Use at most {MAX_PASSWORD_LENGTH} characters for your password.")
    return password


def _auth_config(settings: Settings) -> tuple[str, str]:
    url = settings.supabase_url.rstrip("/")
    key = settings.supabase_service_role_key.get_secret_value()
    if not url or not key or not settings.anonymous_token_secret.get_secret_value():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Sign-in is not configured.")
    return url, key


async def _auth_request(settings: Settings, method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> httpx.Response:
    url, key = _auth_config(settings)
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=SUPABASE_AUTH_TIMEOUT_SECONDS) as client:
            return await client.request(method, f"{url}/auth/v1{path}", json=json, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not reach the sign-in service. Please try again.") from exc


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    return str(payload.get("msg") or payload.get("error_description") or payload.get("message") or payload.get("error") or "")


def _error_code(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error_code") or "")
    except ValueError:
        return ""


def _user_from(payload: dict) -> tuple[str, str]:
    user = payload.get("user") if "user" in payload else payload
    user_id, email = (user or {}).get("id"), (user or {}).get("email")
    if not user_id or not email:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="The sign-in service returned no account.")
    return str(user_id), str(email).lower()


async def sign_up(email: str, password: str, settings: Settings) -> tuple[str, str]:
    response = await _auth_request(settings, "POST", "/admin/users", json={"email": email, "password": password, "email_confirm": True})
    if response.status_code == 422 and ("exist" in _error_code(response) or "already" in _error_text(response).lower()):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists. Log in instead.")
    if response.status_code == 422 and "password" in _error_text(response).lower():
        raise HTTPException(status_code=422, detail=_error_text(response))
    if response.status_code >= 400:
        log.warning("Supabase sign-up failed: %s %s", response.status_code, response.text[:300])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_error_text(response) or "Could not create your account.")
    return _user_from(response.json())


async def log_in(email: str, password: str, settings: Settings) -> tuple[str, str]:
    now = time.monotonic()
    recent = [failed for failed in _failed_logins.get(email, []) if now - failed < FAILED_LOGIN_WINDOW_SECONDS]
    if len(recent) >= MAX_FAILED_LOGINS:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many wrong passwords. Please try again in 15 minutes.")
    response = await _auth_request(settings, "POST", "/token", params={"grant_type": "password"}, json={"email": email, "password": password})
    if response.status_code in (400, 401):
        _failed_logins[email] = [*recent, now]
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="That email and password don't match.")
    if response.status_code == 429:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many sign-in attempts. Please wait a minute and try again.")
    if response.status_code >= 400:
        log.warning("Supabase password sign-in failed: %s %s", response.status_code, response.text[:300])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_error_text(response) or "Could not sign you in.")
    _failed_logins.pop(email, None)
    return _user_from(response.json())


async def set_password(email: str, password: str, settings: Settings) -> tuple[str, str]:
    """Admin: give an existing account (for example one made with an email code) a password, or create it."""
    listing = await _auth_request(settings, "GET", "/admin/users", params={"page": 1, "per_page": 1000})
    if listing.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_error_text(listing) or "Could not read accounts.")
    users = listing.json().get("users", [])
    match = next((user for user in users if str(user.get("email", "")).lower() == email), None)
    if match is None:
        return await sign_up(email, password, settings)
    response = await _auth_request(settings, "PUT", f"/admin/users/{match['id']}", json={"password": password, "email_confirm": True})
    if response.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_error_text(response) or "Could not set the password.")
    return _user_from(response.json())
