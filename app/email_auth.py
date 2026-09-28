"""Email sign-in through Supabase Auth one-time codes, exchanged for a FitCart session token.

When BREVO_API_KEY (or SMTP_*) is set the API sends the code email itself: Supabase's admin API
creates the code without emailing it, and we deliver it through Brevo's HTTPS API (Render's free plan
blocks outbound SMTP ports) or SMTP. That avoids Supabase's mailer and its templates and surfaces the
real delivery error. Without either, Supabase sends its own email. Either way Supabase checks the code.
"""

import asyncio
import logging
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr

import httpx
from fastapi import HTTPException, status

from app.config import Settings

log = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SUPABASE_AUTH_TIMEOUT_SECONDS = 15
SMTP_TIMEOUT_SECONDS = 20
BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"
RESEND_WAIT_SECONDS = 45
_last_sent: dict[str, float] = {}  # email -> time of the last code we emailed (per process)


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
        await _send_code_ourselves(email, settings)
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


async def _send_code_ourselves(email: str, settings: Settings) -> None:
    waited = time.monotonic() - _last_sent.get(email, 0)
    if waited < RESEND_WAIT_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"We just sent a code. Please wait {int(RESEND_WAIT_SECONDS - waited) + 1} seconds before asking for another.",
        )
    # Magic-link type creates the account first when the email is new; Supabase does not send anything here.
    response = await _auth_request(settings, "POST", "/admin/generate_link", json={"type": "magiclink", "email": email})
    if response.status_code >= 400:
        log.warning("Supabase generate_link failed: %s %s", response.status_code, response.text[:300])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_error_text(response) or "Could not create a sign-in code.")
    payload = response.json()
    code = payload.get("email_otp") or (payload.get("properties") or {}).get("email_otp")
    if not code:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="The sign-in service returned no code.")
    if settings.code_email_sender == "brevo":
        await _deliver_with_brevo(email, str(code), settings)
        _last_sent[email] = time.monotonic()
        return
    try:
        await asyncio.to_thread(_deliver, email, str(code), settings)
    except (smtplib.SMTPException, OSError) as exc:
        log.error("Sending the sign-in code to %s failed: %r", email, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not send the sign-in email ({_smtp_reason(exc)}).") from exc
    _last_sent[email] = time.monotonic()


def _smtp_reason(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "the email account rejected the SMTP username or app password"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "that email address was refused"
    if isinstance(exc, (TimeoutError, OSError)) and not isinstance(exc, smtplib.SMTPException):
        return "could not connect to the email server"
    return str(exc)[:160] or exc.__class__.__name__


def _code_subject(code: str) -> str:
    return f"{code} is your FitCart code"


def _code_text(code: str) -> str:
    return f"Your FitCart sign-in code is {code}\n\nEnter it in FitCart. It expires in 1 hour.\nDidn't ask for this? You can ignore this email.\n"


def _code_html(code: str) -> str:
    return f"""<div style="font-family:Arial,sans-serif;max-width:420px;margin:auto;padding:24px;color:#17121c">
<h2 style="margin:0 0 12px">Your FitCart sign-in code</h2>
<p style="font-size:34px;font-weight:700;letter-spacing:8px;margin:12px 0">{code}</p>
<p style="margin:0 0 8px">Enter this code in FitCart. It expires in 1 hour.</p>
<p style="color:#8a7f8d;font-size:13px;margin:16px 0 0">Didn't ask for this? You can ignore this email.</p>
</div>"""


async def _deliver_with_brevo(email: str, code: str, settings: Settings) -> None:
    body = {
        "sender": {"name": settings.email_sender_name, "email": settings.email_sender},
        "to": [{"email": email}],
        "subject": _code_subject(code),
        "htmlContent": _code_html(code),
        "textContent": _code_text(code),
    }
    headers = {"api-key": settings.brevo_api_key.get_secret_value(), "accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=SMTP_TIMEOUT_SECONDS) as client:
            response = await client.post(BREVO_SEND_URL, json=body, headers=headers)
    except httpx.HTTPError as exc:
        log.error("Brevo request failed: %r", exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not reach the email service. Please try again.") from exc
    if response.status_code >= 400:
        try:
            reason = response.json().get("message") or response.text
        except ValueError:
            reason = response.text
        log.error("Brevo rejected the sign-in email: %s %s", response.status_code, reason[:300])
        hint = " Check BREVO_API_KEY." if response.status_code == 401 else ""
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not send the sign-in email ({reason[:160]}).{hint}")


def _code_message(email: str, code: str, settings: Settings) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = _code_subject(code)
    message["From"] = formataddr((settings.email_sender_name, settings.email_sender or settings.smtp_username))
    message["To"] = email
    message.set_content(_code_text(code))
    message.add_alternative(_code_html(code), subtype="html")
    return message


def _deliver(email: str, code: str, settings: Settings) -> None:
    message = _code_message(email, code, settings)
    password = settings.smtp_password.get_secret_value().replace(" ", "")  # Google shows app passwords in groups of four
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS, context=context) as server:
            server.login(settings.smtp_username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as server:
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
