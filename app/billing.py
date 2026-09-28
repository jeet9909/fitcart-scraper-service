"""Razorpay payments for passes and plans, turned into look grants by the checkout callback or the webhook."""

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import HTTPException, status

from app.config import Settings
from app.looks import LookLedger

log = logging.getLogger(__name__)

RAZORPAY_API = "https://api.razorpay.com/v1"


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
# Razorpay needs an end for subscriptions: renew for up to 10 years.
TOTAL_CYCLES = {"monthly": 120, "yearly": 10}


def _hmac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signature_matches(secret: str, message: bytes, signature: str | None) -> bool:
    return bool(signature) and hmac.compare_digest(_hmac(secret, message), signature)


def _add_months(moment: datetime, months: int) -> datetime:
    month = moment.month - 1 + months
    year, month = moment.year + month // 12, month % 12 + 1
    days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return moment.replace(year=year, month=month, day=min(moment.day, days))


def _stamp(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


class Billing:
    _plan_ids: dict[str, str] = {}  # Razorpay plan ids, found or created once per process

    def __init__(self, settings: Settings, ledger: LookLedger) -> None:
        self.settings = settings
        self.ledger = ledger

    @property
    def key_id(self) -> str:
        return self.settings.razorpay_key_id.strip()

    @property
    def secret(self) -> str:
        return self.settings.razorpay_key_secret.get_secret_value()

    @property
    def test_mode(self) -> bool:
        return not self.key_id.startswith("rzp_live_")

    @property
    def enabled(self) -> bool:
        return bool(self.key_id and self.secret) and (self.key_id.startswith("rzp_test_") or self.settings.razorpay_allow_live)

    def _require(self) -> None:
        if not (self.key_id and self.secret):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Payments are not set up yet.")
        if not self.enabled:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="A live Razorpay key is configured but live payments are switched off.")

    async def _razorpay(self, method: str, path: str, body: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> dict:
        self._require()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(method, f"{RAZORPAY_API}{path}", json=body, params=params, auth=(self.key_id, self.secret))
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not reach Razorpay. Please try again.") from exc
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            message = (payload.get("error") or {}).get("description") or "Razorpay rejected the request."
            log.warning("Razorpay %s %s failed: %s", method, path, message)
            code = status.HTTP_404_NOT_FOUND if response.status_code == 404 else status.HTTP_502_BAD_GATEWAY
            raise HTTPException(status_code=code, detail=message)
        return payload

    async def _plan_id(self, plan_key: str, billing: str) -> str:
        plan = PLANS[plan_key]
        amount = plan.yearly if billing == "yearly" else plan.monthly
        tag = f"{plan_key}_{billing}_{amount}"
        if tag in self._plan_ids:
            return self._plan_ids[tag]
        existing = await self._razorpay("GET", "/plans", params={"count": 100})
        found = next((p["id"] for p in existing.get("items", []) if (p.get("notes") or {}).get("fitcart") == tag), None)
        if not found:
            created = await self._razorpay("POST", "/plans", {
                "period": billing, "interval": 1,
                "item": {"name": plan.name, "amount": amount, "currency": "INR", "description": f"{plan.looks} looks every month"},
                "notes": {"fitcart": tag},
            })
            found = created["id"]
        self._plan_ids[tag] = found
        return found

    async def create_checkout(self, user_id: str, email: str, plan_key: str, billing: str) -> dict:
        """Create the Razorpay order (pass) or subscription (plans) that the checkout window pays."""
        plan = PLANS[plan_key]
        notes = {"user_id": user_id, "plan": plan_key, "billing": "once" if plan.once else billing}
        options = {"key_id": self.key_id, "name": "FitCart", "email": email, "currency": "INR"}
        if plan.once:
            order = await self._razorpay("POST", "/orders", {"amount": plan.once, "currency": "INR", "receipt": f"pass-{user_id[:8]}", "notes": notes})
            return {**options, "order_id": order["id"], "amount": plan.once, "description": f"{plan.name} · {plan.looks} looks for {plan.days} days"}
        subscription = await self._razorpay("POST", "/subscriptions", {
            "plan_id": await self._plan_id(plan_key, billing), "total_count": TOTAL_CYCLES[billing],
            "quantity": 1, "customer_notify": 1, "notes": notes,
        })
        amount = plan.yearly if billing == "yearly" else plan.monthly
        return {**options, "subscription_id": subscription["id"], "amount": amount, "description": f"{plan.name} · {plan.looks} looks every month, billed {billing}"}

    async def confirm(self, user_id: str, payment_id: str, signature: str, order_id: str | None = None, subscription_id: str | None = None) -> bool:
        """Check the checkout callback and add looks. Returns False when Razorpay has not finished charging yet."""
        self._require()
        if bool(order_id) == bool(subscription_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Send either an order or a subscription id.")
        message = f"{order_id}|{payment_id}" if order_id else f"{payment_id}|{subscription_id}"
        if not signature_matches(self.secret, message.encode(), signature):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The payment could not be verified.")
        if order_id:
            order = await self._razorpay("GET", f"/orders/{order_id}")
            self._check_owner(order, user_id)
            payment = await self._razorpay("GET", f"/payments/{payment_id}")
            if payment.get("order_id") != order_id or payment.get("amount") != order.get("amount"):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The payment does not match this order.")
            if payment.get("status") == "authorized":
                payment = await self._razorpay("POST", f"/payments/{payment_id}/capture", {"amount": payment["amount"], "currency": payment.get("currency", "INR")})
            if payment.get("status") != "captured":
                return False
            await self._grant_pass(order)
            return True
        subscription = await self._razorpay("GET", f"/subscriptions/{subscription_id}")
        self._check_owner(subscription, user_id)
        return await self._grant_subscription(subscription)

    @staticmethod
    def _check_owner(entity: dict, user_id: str) -> None:
        if (entity.get("notes") or {}).get("user_id") != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This payment belongs to another account.")

    async def handle_webhook(self, payload: bytes, signature: str | None) -> None:
        secret = self.settings.razorpay_webhook_secret.get_secret_value()
        if not secret:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Razorpay webhook secret is not set.")
        if not signature_matches(secret, payload, signature):
            raise HTTPException(status_code=400, detail="Invalid Razorpay signature")
        event = json.loads(payload)
        body = event.get("payload") or {}
        entity = lambda name: (body.get(name) or {}).get("entity") or {}
        if event.get("event") == "order.paid" and (entity("order").get("notes") or {}).get("plan") == "pass":
            await self._grant_pass(entity("order"))
        elif event.get("event") == "subscription.charged":
            await self._grant_subscription(entity("subscription"))

    async def _grant_pass(self, order: dict) -> None:
        notes = order.get("notes") or {}
        plan = PLANS["pass"]
        start = _stamp(order.get("created_at") or int(datetime.now(UTC).timestamp()))
        await self.ledger.add_grants([{
            "user_id": notes["user_id"], "kind": "pass", "looks": plan.looks, "payment_ref": order["id"],
            "starts_at": start.isoformat(), "expires_at": (start + timedelta(days=plan.days)).isoformat(),
        }])

    async def _grant_subscription(self, subscription: dict) -> bool:
        notes = subscription.get("notes") or {}
        plan_key = notes.get("plan")
        current_start, current_end = subscription.get("current_start"), subscription.get("current_end")
        if plan_key not in ("plus", "pro") or not notes.get("user_id"):
            log.info("Ignoring subscription %s without FitCart plan notes", subscription.get("id"))
            return False
        if subscription.get("status") not in ("active", "completed") or not current_start or not current_end:
            return False
        start, end = _stamp(current_start), _stamp(current_end)
        looks, ref = PLANS[plan_key].looks, f"{subscription['id']}:{current_start}"
        if notes.get("billing") == "yearly":
            # A yearly plan still refreshes monthly: one grant per month of the paid year.
            months = [(_add_months(start, i), min(_add_months(start, i + 1), end)) for i in range(12)]
            rows = [
                {"user_id": notes["user_id"], "kind": plan_key, "looks": looks, "payment_ref": f"{ref}:{i}",
                 "starts_at": s.isoformat(), "expires_at": e.isoformat()}
                for i, (s, e) in enumerate(months) if s < end
            ]
        else:
            rows = [{"user_id": notes["user_id"], "kind": plan_key, "looks": looks, "payment_ref": ref,
                     "starts_at": start.isoformat(), "expires_at": end.isoformat()}]
        await self.ledger.add_grants(rows)
        return True
