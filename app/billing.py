"""Stripe Checkout for passes and plans, turned into look grants by the webhook or the return-page check."""

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status

from app.config import Settings
from app.looks import LookLedger

log = logging.getLogger(__name__)

STRIPE_API = "https://api.stripe.com/v1"
SIGNATURE_TOLERANCE_SECONDS = 300


@dataclass(frozen=True)
class Plan:
    name: str
    looks: int
    monthly: int = 0  # paise
    yearly: int = 0
    once: int = 0
    days: int = 0


# Prices include GST, in paise. Plus and Pro looks refresh every month, also on yearly billing.
PLANS: dict[str, Plan] = {
    "pass": Plan(name="FitCart Occasion Pass", looks=10, once=12_900, days=7),
    "plus": Plan(name="FitCart Plus", looks=25, monthly=34_900, yearly=329_900),
    "pro": Plan(name="FitCart Pro", looks=60, monthly=79_900, yearly=749_900),
}


def _flatten(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: {"a": {"b": 1}} -> a[b]=1, lists as a[0][b]=1."""
    pairs: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            pairs += _flatten(value, name)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                pairs += _flatten(item, f"{name}[{index}]") if isinstance(item, dict) else [(f"{name}[{index}]", str(item))]
        elif value is not None:
            pairs.append((name, str(value).lower() if isinstance(value, bool) else str(value)))
    return pairs


def verify_signature(payload: bytes, header: str, secret: str, now: float | None = None) -> None:
    """Check the Stripe-Signature header (t=timestamp,v1=hmac) the way Stripe's SDKs do."""
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature") from exc
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in parts.get("v1", [])):
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")
    if abs((now or time.time()) - timestamp) > SIGNATURE_TOLERANCE_SECONDS:
        raise HTTPException(status_code=400, detail="Stripe signature is too old")


def _add_months(moment: datetime, months: int) -> datetime:
    month = moment.month - 1 + months
    year, month = moment.year + month // 12, month % 12 + 1
    days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return moment.replace(year=year, month=month, day=min(moment.day, days))


def _stamp(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


class Billing:
    def __init__(self, settings: Settings, ledger: LookLedger) -> None:
        self.settings = settings
        self.ledger = ledger

    @property
    def key(self) -> str:
        return self.settings.stripe_secret_key.get_secret_value()

    @property
    def enabled(self) -> bool:
        return bool(self.key) and (self.key.startswith("sk_test_") or self.settings.stripe_allow_live)

    @property
    def test_mode(self) -> bool:
        return not self.key.startswith("sk_live_")

    def _require(self) -> None:
        if not self.key:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Payments are not set up yet.")
        if not self.enabled:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="A live Stripe key is configured but live payments are switched off.")

    async def _stripe(self, method: str, path: str, data: dict[str, Any] | None = None) -> dict:
        self._require()
        body = urlencode(_flatten(data)) if data else None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(
                    method, f"{STRIPE_API}{path}", content=body,
                    headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not reach Stripe. Please try again.") from exc
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            message = (payload.get("error") or {}).get("message") or "Stripe rejected the request."
            log.warning("Stripe %s %s failed: %s", method, path, message)
            code = status.HTTP_404_NOT_FOUND if response.status_code == 404 else status.HTTP_502_BAD_GATEWAY
            raise HTTPException(status_code=code, detail=message)
        return payload

    async def create_checkout(self, user_id: str, email: str, plan_key: str, billing: str) -> str:
        plan = PLANS[plan_key]
        subscription = not plan.once
        amount = plan.once or (plan.yearly if billing == "yearly" else plan.monthly)
        metadata = {"user_id": user_id, "plan": plan_key, "billing": billing if subscription else "once"}
        price_data: dict[str, Any] = {"currency": "inr", "unit_amount": amount, "product_data": {"name": plan.name}}
        if subscription:
            price_data["recurring"] = {"interval": "year" if billing == "yearly" else "month"}
        app_url = self.settings.app_url.rstrip("/") + "/"
        data: dict[str, Any] = {
            "mode": "subscription" if subscription else "payment",
            "line_items": [{"quantity": 1, "price_data": price_data}],
            "success_url": f"{app_url}?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{app_url}?checkout=cancelled",
            "customer_email": email,
            "client_reference_id": user_id,
            "metadata": metadata,
        }
        if subscription:
            data["subscription_data"] = {"metadata": metadata}
        else:
            data["payment_intent_data"] = {"metadata": metadata}
        session = await self._stripe("POST", "/checkout/sessions", data)
        return session["url"]

    async def confirm(self, user_id: str, session_id: str) -> None:
        """Called when the buyer returns from Checkout, so looks appear even before (or without) the webhook."""
        session = await self._stripe("GET", f"/checkout/sessions/{session_id}")
        if (session.get("client_reference_id") or (session.get("metadata") or {}).get("user_id")) != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This payment belongs to another account.")
        if session.get("status") != "complete" or session.get("payment_status") not in ("paid", "no_payment_required"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The payment has not gone through yet.")
        await self._grant_from_session(session)

    async def handle_webhook(self, payload: bytes, signature: str | None) -> None:
        secret = self.settings.stripe_webhook_secret.get_secret_value()
        if not secret:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Stripe webhook secret is not set.")
        verify_signature(payload, signature or "", secret)
        event = json.loads(payload)
        kind, obj = event.get("type"), (event.get("data") or {}).get("object") or {}
        if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            if obj.get("mode") == "payment" and obj.get("payment_status") == "paid":
                await self._grant_from_session(obj)
        elif kind == "invoice.paid":
            await self._grant_from_invoice(obj)

    async def _grant_from_session(self, session: dict) -> None:
        metadata = session.get("metadata") or {}
        user_id = session.get("client_reference_id") or metadata.get("user_id")
        if session.get("mode") == "payment":
            plan = PLANS["pass"]
            start = _stamp(session.get("created") or int(time.time()))
            await self.ledger.add_grants([{
                "user_id": user_id, "kind": "pass", "looks": plan.looks, "stripe_ref": session["id"],
                "starts_at": start.isoformat(), "expires_at": (start + timedelta(days=plan.days)).isoformat(),
            }])
            return
        invoice_id = session.get("invoice")
        if isinstance(invoice_id, dict):
            invoice_id = invoice_id.get("id")
        if invoice_id:
            await self._grant_from_invoice(await self._stripe("GET", f"/invoices/{invoice_id}"), fallback=metadata)

    async def _grant_from_invoice(self, invoice: dict, fallback: dict | None = None) -> None:
        if invoice.get("status") != "paid":
            return
        parent = (invoice.get("parent") or {}).get("subscription_details") or {}
        metadata = (invoice.get("subscription_details") or {}).get("metadata") or parent.get("metadata") or fallback or {}
        if not metadata.get("user_id"):
            subscription_id = invoice.get("subscription") or parent.get("subscription")
            if isinstance(subscription_id, dict):
                subscription_id = subscription_id.get("id")
            if subscription_id:
                metadata = (await self._stripe("GET", f"/subscriptions/{subscription_id}")).get("metadata") or {}
        plan_key = metadata.get("plan")
        if not metadata.get("user_id") or plan_key not in ("plus", "pro"):
            log.info("Ignoring paid invoice %s without FitCart plan metadata", invoice.get("id"))
            return
        lines = (invoice.get("lines") or {}).get("data") or []
        period = next((line.get("period") for line in lines if line.get("period")), None)
        if not period:
            return
        start, end = _stamp(period["start"]), _stamp(period["end"])
        looks = PLANS[plan_key].looks
        if metadata.get("billing") == "yearly":
            # A yearly plan still refreshes monthly: one grant per month of the paid year.
            months = [(_add_months(start, i), min(_add_months(start, i + 1), end)) for i in range(12)]
            rows = [
                {"user_id": metadata["user_id"], "kind": plan_key, "looks": looks, "stripe_ref": f"{invoice['id']}:{i}",
                 "starts_at": s.isoformat(), "expires_at": e.isoformat()}
                for i, (s, e) in enumerate(months) if s < end
            ]
        else:
            rows = [{"user_id": metadata["user_id"], "kind": plan_key, "looks": looks, "stripe_ref": invoice["id"],
                     "starts_at": start.isoformat(), "expires_at": end.isoformat()}]
        await self.ledger.add_grants(rows)
