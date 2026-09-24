from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from fastapi import HTTPException, status

from app.config import Settings
from app.models import AnonymousSessionResponse, EmailSessionResponse


def create_anonymous_session(settings: Settings) -> AnonymousSessionResponse:
    secret = settings.anonymous_token_secret.get_secret_value()
    if not secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Anonymous sessions are not configured")
    user_id = str(uuid4())
    expires_at = datetime.now(UTC) + timedelta(days=settings.anonymous_token_days)
    token = jwt.encode({"sub": user_id, "exp": expires_at, "type": "anonymous"}, secret, algorithm="HS256")
    return AnonymousSessionResponse(anonymous_user_id=user_id, access_token=token, expires_at=expires_at)


def create_email_session(user_id: str, email: str, settings: Settings) -> EmailSessionResponse:
    """Session for a signed-in email account; the user id is the Supabase Auth id, so looks follow the account."""
    secret = settings.anonymous_token_secret.get_secret_value()
    if not secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Sessions are not configured")
    user_id = str(UUID(user_id))
    expires_at = datetime.now(UTC) + timedelta(days=settings.anonymous_token_days)
    token = jwt.encode({"sub": user_id, "exp": expires_at, "type": "email", "email": email}, secret, algorithm="HS256")
    return EmailSessionResponse(
        anonymous_user_id=user_id, access_token=token, expires_at=expires_at, email=email, unlimited=settings.is_unlimited(email)
    )


def verify_session_token(token: str, settings: Settings) -> dict:
    """Claims of a valid anonymous or email session token."""
    try:
        payload = jwt.decode(token, settings.anonymous_token_secret.get_secret_value(), algorithms=["HS256"])
        if payload.get("type") not in ("anonymous", "email"):
            raise ValueError("wrong token type")
        payload["sub"] = str(UUID(payload["sub"]))
        return payload
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired anonymous token") from exc


def verify_anonymous_token(token: str, settings: Settings) -> str:
    return verify_session_token(token, settings)["sub"]
