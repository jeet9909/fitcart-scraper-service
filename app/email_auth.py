"""Email sign-in through Supabase Auth one-time codes, exchanged for a FitCart session token."""

import re

import httpx
from fastapi import HTTPException, status

from app.config import Settings

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SUPABASE_AUTH_TIMEOUT_SECONDS = 15


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_PATTERN.match(email) or len(email) > 254:
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    return email


def _auth_config(settings: Settings) -> tuple[str, str]:
    url = settings.supabase_url.rstrip("/")
    key = settings.supabase_service_role_key.get_secret_value()
    if not url or not key or not settings.anonymous_token_secret.get_secret_value():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Email sign-in is not configured.")
    return url, key


async def _auth_request(settings: Settings, method: str, path: str, *, json: dict | None = None, user_token: str | None = None) -> httpx.Response:
    url, key = _auth_config(settings)
    headers = {"apikey": key, "Authorization": f"Bearer {user_token or key}"}
    try:
        async with httpx.AsyncClient(timeout=SUPABASE_AUTH_TIMEOUT_SECONDS) as client:
            return await client.request(method, f"{url}/auth/v1{path}", json=json, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not reach the sign-in service. Please try again.") from exc


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    return str(payload.get("msg") or payload.get("error_description") or payload.get("message") or payload.get("error") or "")


async def send_code(email: str, settings: Settings) -> None:
    response = await _auth_request(settings, "POST", "/otp", json={"email": email, "create_user": True})
    if response.status_code == 429:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many sign-in emails. Please wait a minute and try again.")
    if response.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_error_text(response) or "Could not send the sign-in email.")


def _user_from(payload: dict) -> tuple[str, str]:
    user = payload.get("user") if "user" in payload else payload
    user_id, email = (user or {}).get("id"), (user or {}).get("email")
    if not user_id or not email:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="The sign-in service returned no account.")
    return str(user_id), str(email).lower()


async def verify_code(email: str, code: str, settings: Settings) -> tuple[str, str]:
    code = re.sub(r"\s+", "", code)
    if not code.isdigit() or not 6 <= len(code) <= 10:
        raise HTTPException(status_code=422, detail="Enter the code from the email.")
    response = await _auth_request(settings, "POST", "/verify", json={"type": "email", "email": email, "token": code})
    if response.status_code in (400, 401, 403, 422):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="That code is wrong or has expired. Request a new one.")
    if response.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_error_text(response) or "Could not check the code.")
    return _user_from(response.json())


async def user_from_link_token(access_token: str, settings: Settings) -> tuple[str, str]:
    """Read the account behind the access token a Supabase magic link puts in the page URL."""
    response = await _auth_request(settings, "GET", "/user", user_token=access_token)
    if response.status_code in (401, 403):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="That sign-in link has expired. Request a new one.")
    if response.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not check the sign-in link.")
    return _user_from(response.json())
