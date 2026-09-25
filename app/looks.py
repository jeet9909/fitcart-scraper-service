"""Server-side look allowance: free looks every month for signed-in accounts, plus looks bought through Stripe."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException, status

from app.config import Settings
from app.models import LookBalanceResponse, LookGrantSummary
from app.tryon import TryOnService

log = logging.getLogger(__name__)

PAID_KINDS = ("pro", "plus", "pass")


@dataclass
class Reservation:
    grant_id: str | None  # None when the account is unlimited or limits are not set up


class LookLedger:
    def __init__(self, service: TryOnService, settings: Settings) -> None:
        self.service = service
        self.settings = settings
        self._missing_schema_logged = False

    def _schema_missing(self, response) -> bool:
        """PostgREST answers 404 until supabase/schema.sql has created the look_grants table and functions."""
        if response.status_code != 404:
            return False
        if not self._missing_schema_logged:
            log.warning("Look limits are not enforced: run supabase/schema.sql to create look_grants and consume_look")
            self._missing_schema_logged = True
        return True

    def _limits_active(self) -> bool:
        return self.settings.look_limits_enabled and bool(self.settings.supabase_url)

    async def reserve(self, claims: dict) -> Reservation:
        """Spend one look before generating; the caller refunds it if the try-on fails."""
        email = claims.get("email")
        if not self._limits_active() or self.settings.is_unlimited(email):
            return Reservation(None)
        if not email:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "sign_in_required", "message": f"Sign in with your email to get {self.settings.free_looks_per_month} free looks every month."},
            )
        response = await self.service.rest(
            "POST", "rpc/consume_look", json_body={"p_user": claims["sub"], "p_free_looks": self.settings.free_looks_per_month}
        )
        if self._schema_missing(response):
            return Reservation(None)
        if response.status_code >= 400:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not check your looks. Please try again.")
        grant_id = response.json() if response.content.strip() else None
        if not grant_id:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={"code": "no_looks_left", "message": "You have used all your looks. Pick a pass or plan to keep going."},
            )
        return Reservation(str(grant_id))

    async def refund(self, reservation: Reservation) -> None:
        if not reservation.grant_id:
            return
        try:
            await self.service.rest("POST", "rpc/refund_look", json_body={"p_grant": reservation.grant_id})
        except Exception:  # a failed refund must not hide the original error
            log.exception("Could not refund look grant %s", reservation.grant_id)

    async def balance(self, claims: dict) -> LookBalanceResponse:
        email = claims.get("email")
        base = {"signed_in": bool(email), "free_looks_per_month": self.settings.free_looks_per_month}
        if self.settings.is_unlimited(email):
            return LookBalanceResponse(**base, unlimited=True, enforced=self._limits_active())
        if not email or not self._limits_active():
            return LookBalanceResponse(**base, enforced=self._limits_active())
        user_id = claims["sub"]
        ensured = await self.service.rest(
            "POST", "rpc/ensure_free_looks", json_body={"p_user": user_id, "p_looks": self.settings.free_looks_per_month}
        )
        if self._schema_missing(ensured):
            return LookBalanceResponse(**base, enforced=False)
        now = datetime.now(UTC).isoformat()
        response = await self.service.rest(
            "GET",
            "look_grants",
            params={
                "select": "kind,looks,used,expires_at",
                "user_id": f"eq.{user_id}",
                "starts_at": f"lte.{now}",
                "expires_at": f"gt.{now}",
                "order": "expires_at.asc",
            },
        )
        if response.status_code >= 400:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not load your looks. Please try again.")
        rows = response.json()
        grants = [
            LookGrantSummary(kind=row["kind"], remaining=max(0, row["looks"] - row["used"]), total=row["looks"], expires_at=row["expires_at"])
            for row in rows
        ]
        active_paid = {g.kind for g in grants if g.kind in PAID_KINDS}
        plan = next((kind for kind in PAID_KINDS if kind in active_paid), "free")
        return LookBalanceResponse(**base, enforced=True, remaining=sum(g.remaining for g in grants), plan=plan, grants=grants)

    async def add_grants(self, rows: list[dict]) -> None:
        """Insert purchased grants; stripe_ref is unique, so webhook retries and the return-page check never double up."""
        if not rows:
            return
        response = await self.service.rest(
            "POST", "look_grants", params={"on_conflict": "stripe_ref"}, json_body=rows, prefer="resolution=ignore-duplicates,return=minimal"
        )
        if response.status_code >= 400:
            log.error("Could not save look grants: %s %s", response.status_code, response.text[:300])
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not add your looks. Please try again.")
