"""Email + password accounts in Supabase Auth, exchanged for a FitCart session token.

With EMAIL_VERIFICATION_ENABLED off, accounts are created through Supabase's admin API as already confirmed
and no email is sent. With it on, sign-up goes through Supabase's own /signup: Supabase emails a one-time
code (the {{ .Token }} in its "Confirm signup" template) and the account can sign in once the code is
verified. Forgotten passwords use the same mechanism (/recover, "Reset password" template). Supabase sends
the emails through the project's SMTP settings, so a custom SMTP provider must be set up first; its built-in
sender only delivers to the project's own team. Passwords are checked by Supabase (grant_type=password);
FitCart never stores them. Repeated wrong passwords and wrong codes for one email are slowed down here.
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
MAX_FAILED_CODES = 8
_failed_codes: dict[str, list[float]] = {}  # email -> times of recent wrong email codes (per process)
CODE_PATTERN = re.compile(r"^\d{6,10}$")  # Supabase codes are 6 digits by default (up to 10 if changed)


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
    if response.status_code in (400, 401, 403) and (_error_code(response) == "email_not_confirmed" or "not confirmed" in _error_text(response).lower()):
        # Right password, email not verified yet: send a fresh code and let the app show the code screen.
        try:
            await resend_code(email, settings)
        except HTTPException:
            pass  # a code was sent moments ago; the one in the inbox still works
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"code": "email_not_verified", "message": "Confirm your email first. We've sent you a new code."})
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


# ---------------------------------------------------------------- email codes (EMAIL_VERIFICATION_ENABLED)


def _verification_on(settings: Settings) -> None:
    if not settings.email_verification_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": "email_codes_off", "message": "Email codes are not switched on yet. Write to us from Help and we'll sort out your account."})


def _check_code(email: str, code: str) -> str:
    code = re.sub(r"\s", "", code or "")
    if not CODE_PATTERN.match(code):
        raise HTTPException(status_code=422, detail={"code": "invalid_code", "message": "Enter the code from the email (numbers only)."})
    now = time.monotonic()
    recent = [failed for failed in _failed_codes.get(email, []) if now - failed < FAILED_LOGIN_WINDOW_SECONDS]
    _failed_codes[email] = recent
    if len(recent) >= MAX_FAILED_CODES:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail={"code": "too_many_codes", "message": "Too many wrong codes. Ask for a new code in 15 minutes."})
    return code


def _email_limit(response: httpx.Response) -> HTTPException | None:
    if response.status_code == 429 or "rate limit" in _error_text(response).lower() or _error_code(response) in ("over_email_send_rate_limit", "over_request_rate_limit"):
        return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                             detail={"code": "email_rate_limited", "message": "We've just sent you a code. Please wait a minute before asking for another."})
    return None


async def start_sign_up(email: str, password: str, settings: Settings) -> None:
    """Create an unconfirmed account; Supabase emails the confirmation code."""
    response = await _auth_request(settings, "POST", "/signup", json={"email": email, "password": password})
    if limited := _email_limit(response):
        raise limited
    if response.status_code == 422 and ("exist" in _error_code(response) or "already" in _error_text(response).lower()):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists. Log in instead.")
    if response.status_code == 422 and "password" in _error_text(response).lower():
        raise HTTPException(status_code=422, detail=_error_text(response))
    if response.status_code >= 400:
        log.warning("Supabase sign-up failed: %s %s", response.status_code, response.text[:300])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_error_text(response) or "Could not create your account.")
    payload = response.json()
    user = payload.get("user") if "user" in payload else payload
    # Supabase answers a sign-up for an email that is already confirmed with an empty identity list (and sends nothing).
    if isinstance(user, dict) and user.get("identities") == []:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists. Log in instead.")


async def resend_code(email: str, settings: Settings) -> None:
    response = await _auth_request(settings, "POST", "/resend", json={"type": "signup", "email": email})
    if limited := _email_limit(response):
        raise limited
    if response.status_code >= 400:  # unknown or already confirmed emails are not revealed
        log.info("Supabase resend for an email answered %s %s", response.status_code, response.text[:200])


async def _verify(email: str, code: str, kinds: tuple[str, ...], settings: Settings) -> tuple[str, str]:
    code = _check_code(email, code)
    response = None
    for kind in kinds:
        response = await _auth_request(settings, "POST", "/verify", json={"type": kind, "email": email, "token": code})
        if response.status_code < 400:
            _failed_codes.pop(email, None)
            return _user_from(response.json())
        if response.status_code == 429:
            break
    _failed_codes.setdefault(email, []).append(time.monotonic())
    if response is not None and response.status_code == 429:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail={"code": "too_many_codes", "message": "Too many tries. Please wait a minute."})
    raise HTTPException(status_code=400, detail={"code": "invalid_code",
                                                 "message": "That code is wrong or has expired. Check the latest email, or ask for a new code."})


async def verify_sign_up(email: str, code: str, settings: Settings) -> tuple[str, str]:
    return await _verify(email, code, ("email", "signup"), settings)


async def send_reset_code(email: str, settings: Settings) -> None:
    response = await _auth_request(settings, "POST", "/recover", json={"email": email})
    if limited := _email_limit(response):
        raise limited
    if response.status_code >= 400:  # never reveal whether the email has an account
        log.info("Supabase recover answered %s %s", response.status_code, response.text[:200])


async def reset_password(email: str, code: str, password: str, settings: Settings) -> tuple[str, str]:
    user_id, email = await _verify(email, code, ("recovery",), settings)
    response = await _auth_request(settings, "PUT", f"/admin/users/{user_id}", json={"password": password})
    if response.status_code >= 400:
        log.warning("Password reset could not be saved: %s %s", response.status_code, response.text[:300])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not save your new password. Please try again.")
    _failed_logins.pop(email, None)
    return user_id, email
