"""Email sign-in with one-time codes, exchanged for a FitCart session token.

When a mail account is configured the code system is our own: the API creates a 6-digit code, keeps
only an HMAC of it in memory for 10 minutes, allows 5 tries and one new code every 45 seconds, and
emails it from your Gmail account through the Gmail API (HTTPS, which Render's free plan allows) or
over SMTP on hosts that allow SMTP. After a correct code the account is found or created in Supabase.
With no mail account configured, Supabase Auth sends and checks the code as before.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import secrets
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

import httpx
from fastapi import HTTPException, status

from app.config import Settings

log = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SUPABASE_AUTH_TIMEOUT_SECONDS = 15
MAIL_TIMEOUT_SECONDS = 20
CODE_TTL_SECONDS = 600
MAX_ATTEMPTS = 5
RESEND_WAIT_SECONDS = 45
MAX_SENDS_PER_HOUR = 6
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


@dataclass
class _Pending:
    digest: str
    expires: float
    attempts: int = 0


# Per process. Render runs one instance; a restart just means asking for a new code.
_pending: dict[str, _Pending] = {}
_sends: dict[str, list[float]] = {}
_gmail_token: tuple[str, float] | None = None


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


async def _auth_request(
    settings: Settings, method: str, path: str, *, json: dict | None = None, user_token: str | None = None, params: dict | None = None
) -> httpx.Response:
    url, key = _auth_config(settings)
    headers = {"apikey": key, "Authorization": f"Bearer {user_token or key}"}
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


async def send_code(email: str, settings: Settings) -> None:
    if settings.code_email_sender:
        await _send_own_code(email, settings)
        return
    # redirect_to sends the link in the email back to the site instead of Supabase's Site URL
    # (localhost:3000 until it is changed). Supabase only honours it for URLs in its Redirect URLs list.
    response = await _auth_request(
        settings, "POST", "/otp", json={"email": email, "create_user": True}, params={"redirect_to": settings.auth_redirect_url}
    )
    if response.status_code == 429:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many sign-in emails. Please wait a minute and try again.")
    if response.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_error_text(response) or "Could not send the sign-in email.")


def _digest(email: str, code: str, settings: Settings) -> str:
    key = settings.anonymous_token_secret.get_secret_value().encode()
    return hmac.new(key, f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


async def _send_own_code(email: str, settings: Settings) -> None:
    now = time.monotonic()
    recent = [sent for sent in _sends.get(email, []) if now - sent < 3600]
    if recent and now - recent[-1] < RESEND_WAIT_SECONDS:
        wait = int(RESEND_WAIT_SECONDS - (now - recent[-1])) + 1
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=f"We just sent a code. Please wait {wait} seconds before asking for another.")
    if len(recent) >= MAX_SENDS_PER_HOUR:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many codes for this email. Please try again in an hour.")
    if not settings.anonymous_token_secret.get_secret_value():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Email sign-in is not configured.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    message = _code_message(email, code, settings)
    if settings.code_email_sender == "gmail":
        await _deliver_with_gmail(message, settings)
    else:
        try:
            await asyncio.to_thread(_deliver_with_smtp, message, settings)
        except (smtplib.SMTPException, OSError) as exc:
            log.error("Sending the sign-in code to %s failed: %r", email, exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not send the sign-in email ({_smtp_reason(exc)}).") from exc
    # A new code replaces any earlier one for this email.
    _pending[email] = _Pending(digest=_digest(email, code, settings), expires=time.monotonic() + CODE_TTL_SECONDS)
    _sends[email] = [*recent, time.monotonic()]


def _check_own_code(email: str, code: str, settings: Settings) -> None:
    pending = _pending.get(email)
    if pending is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="That code has expired. Request a new one.")
    if time.monotonic() > pending.expires:
        _pending.pop(email, None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="That code has expired. Request a new one.")
    if hmac.compare_digest(pending.digest, _digest(email, code, settings)):
        _pending.pop(email, None)
        return
    pending.attempts += 1
    if pending.attempts >= MAX_ATTEMPTS:
        _pending.pop(email, None)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many wrong codes. Request a new one.")
    left = MAX_ATTEMPTS - pending.attempts
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"That code is wrong. {left} {'try' if left == 1 else 'tries'} left.")


async def _account_for(email: str, settings: Settings) -> tuple[str, str]:
    """Find or create the Supabase account for a verified email, without Supabase sending anything."""
    response = await _auth_request(settings, "POST", "/admin/generate_link", json={"type": "magiclink", "email": email})
    if response.status_code in (404, 422):
        created = await _auth_request(settings, "POST", "/admin/users", json={"email": email, "email_confirm": True})
        if created.status_code < 400:
            return _user_from(created.json())
        response = await _auth_request(settings, "POST", "/admin/generate_link", json={"type": "magiclink", "email": email})
    if response.status_code >= 400:
        log.warning("Supabase account lookup failed: %s %s", response.status_code, response.text[:300])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not open your account. Please try again.")
    return _user_from(response.json())


async def _gmail_access_token(settings: Settings) -> str:
    global _gmail_token
    if _gmail_token and time.monotonic() < _gmail_token[1]:
        return _gmail_token[0]
    data = {
        "client_id": settings.gmail_client_id,
        "client_secret": settings.gmail_client_secret.get_secret_value(),
        "refresh_token": settings.gmail_refresh_token.get_secret_value(),
        "grant_type": "refresh_token",
    }
    try:
        async with httpx.AsyncClient(timeout=MAIL_TIMEOUT_SECONDS) as client:
            response = await client.post(GOOGLE_TOKEN_URL, data=data)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not reach Google to send the email. Please try again.") from exc
    payload = response.json() if response.content else {}
    if response.status_code >= 400 or "access_token" not in payload:
        reason = payload.get("error_description") or payload.get("error") or response.text[:120]
        log.error("Google token refresh failed: %s %s", response.status_code, reason)
        hint = " The Gmail refresh token has expired or was revoked: create a new one." if payload.get("error") == "invalid_grant" else ""
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not send the sign-in email (Google: {reason}).{hint}")
    _gmail_token = (payload["access_token"], time.monotonic() + int(payload.get("expires_in", 3600)) - 120)
    return _gmail_token[0]


async def _deliver_with_gmail(message: EmailMessage, settings: Settings) -> None:
    global _gmail_token
    token = await _gmail_access_token(settings)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    try:
        async with httpx.AsyncClient(timeout=MAIL_TIMEOUT_SECONDS) as client:
            response = await client.post(GMAIL_SEND_URL, json={"raw": raw}, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not reach Gmail. Please try again.") from exc
    if response.status_code == 401:
        _gmail_token = None  # expired early; the next request refreshes it
    if response.status_code >= 400:
        try:
            reason = (response.json().get("error") or {}).get("message") or response.text
        except ValueError:
            reason = response.text
        log.error("Gmail refused the sign-in email: %s %s", response.status_code, reason[:300])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not send the sign-in email (Gmail: {reason[:160]}).")


def _smtp_reason(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "the email account rejected the SMTP username or app password"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "that email address was refused"
    if isinstance(exc, (TimeoutError, OSError)) and not isinstance(exc, smtplib.SMTPException):
        return "could not connect to the email server"
    return str(exc)[:160] or exc.__class__.__name__


def _code_message(email: str, code: str, settings: Settings) -> EmailMessage:
    minutes = CODE_TTL_SECONDS // 60
    message = EmailMessage()
    message["Subject"] = f"{code} is your FitCart code"
    message["From"] = formataddr((settings.email_sender_name, settings.email_sender or settings.smtp_username))
    message["To"] = email
    message.set_content(f"Your FitCart sign-in code is {code}\n\nEnter it in FitCart. It expires in {minutes} minutes.\nDidn't ask for this? You can ignore this email.\n")
    message.add_alternative(
        f"""<div style="font-family:Arial,sans-serif;max-width:420px;margin:auto;padding:24px;color:#17121c">
<h2 style="margin:0 0 12px">Your FitCart sign-in code</h2>
<p style="font-size:34px;font-weight:700;letter-spacing:8px;margin:12px 0">{code}</p>
<p style="margin:0 0 8px">Enter this code in FitCart. It expires in {minutes} minutes.</p>
<p style="color:#8a7f8d;font-size:13px;margin:16px 0 0">Didn't ask for this? You can ignore this email.</p>
</div>""",
        subtype="html",
    )
    return message


def _deliver_with_smtp(message: EmailMessage, settings: Settings) -> None:
    password = settings.smtp_password.get_secret_value().replace(" ", "")  # Google shows app passwords in groups of four
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=MAIL_TIMEOUT_SECONDS, context=context) as server:
            server.login(settings.smtp_username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=MAIL_TIMEOUT_SECONDS) as server:
            server.starttls(context=context)
            server.login(settings.smtp_username, password)
            server.send_message(message)


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
    if settings.code_email_sender:
        _check_own_code(email, code, settings)
        return await _account_for(email, settings)
    for kind in ("email", "magiclink", "signup"):
        response = await _auth_request(settings, "POST", "/verify", json={"type": kind, "email": email, "token": code})
        if response.status_code not in (400, 401, 403, 422):
            break
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
