"""Google Vertex AI Virtual Try-On (virtual-try-on-001) for looks in the person's own pose.

Unlike Gemini, which redraws the whole person, this model repaints only the clothes on the person's own
photo, so body size, head size, face, glasses and pose stay exactly as photographed. It dresses one
product per request, so an outfit is applied piece by piece, each step starting from the last result.

Setup: a Google Cloud project with the Vertex AI API on, and a service account with the Vertex AI User
role whose JSON key is set as GOOGLE_SERVICE_ACCOUNT_JSON (see README).
"""

import asyncio
import base64
import json
import logging
from typing import Any

import httpx

from app import activity
from app.config import Settings

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
# Garment kinds the model handles, in the order they are put on. Accessories and jewelry are not
# supported, so looks with them use Gemini instead.
LAYER_ORDER = ("bottom wear", "dress or one-piece", "top", "jacket or layer", "footwear")


class VertexTryOnError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class VertexTryOn:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._credentials = None
        self._info: dict[str, Any] | None = None

    @property
    def configured(self) -> bool:
        return self.settings.vertex_tryon_enabled and self._service_account() is not None

    def _service_account(self) -> dict[str, Any] | None:
        if self._info is None:
            raw = self.settings.google_service_account_json.get_secret_value().strip()
            if not raw:
                return None
            try:
                info = json.loads(raw)
            except ValueError:
                log.error("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON; Vertex try-on is off")
                return None
            if info.get("type") != "service_account" or not info.get("private_key"):
                log.error("GOOGLE_SERVICE_ACCOUNT_JSON is not a service account key; Vertex try-on is off")
                return None
            self._info = info
        return self._info

    @property
    def project(self) -> str:
        return self.settings.vertex_project_id or str((self._service_account() or {}).get("project_id", ""))

    def supports(self, categories: list[str]) -> bool:
        return bool(categories) and all(category in LAYER_ORDER for category in categories)

    def _token(self) -> str:
        """An OAuth access token for the service account (cached, refreshed before it expires)."""
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        if self._credentials is None:
            self._credentials = service_account.Credentials.from_service_account_info(self._service_account(), scopes=SCOPES)
        if not self._credentials.valid:  # google-auth counts a token as invalid a few minutes before it expires
            self._credentials.refresh(Request())
        return self._credentials.token

    async def _dress(self, client: httpx.AsyncClient, person: tuple[bytes, str, str], product: tuple[bytes, str, str]) -> tuple[bytes, str, str]:
        location = self.settings.vertex_location
        url = (f"https://{location}-aiplatform.googleapis.com/v1/projects/{self.project}/locations/{location}"
               f"/publishers/google/models/{self.settings.vertex_tryon_model}:predict")
        body = {
            "instances": [{
                "personImage": {"image": {"bytesBase64Encoded": base64.b64encode(person[0]).decode()}},
                "productImages": [{"image": {"bytesBase64Encoded": base64.b64encode(product[0]).decode()}}],
            }],
            "parameters": {"sampleCount": 1},
        }
        token = await asyncio.to_thread(self._token)
        activity.count_call("vertex")
        response = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 429:
            raise VertexTryOnError("Google try-on is busy right now. Please try again in a minute.", 429)
        if response.status_code >= 400:
            raise VertexTryOnError(f"Google try-on failed ({response.status_code}): {response.text[:300]}")
        predictions = (response.json() or {}).get("predictions") or []
        image = next((p for p in predictions if p.get("bytesBase64Encoded")), None)
        if image is None:
            reason = next((p.get("raiFilteredReason") for p in predictions if p.get("raiFilteredReason")), None)
            raise VertexTryOnError(f"Google try-on returned no image{f' ({reason})' if reason else ''}")
        mime = image.get("mimeType") or "image/png"
        return base64.b64decode(image["bytesBase64Encoded"]), mime, "jpg" if mime == "image/jpeg" else "png"

    async def dress_outfit(self, person: tuple[bytes, str, str], pieces: list[tuple[tuple[bytes, str, str], str]]) -> tuple[bytes, str, str]:
        """Put each (product image, category) on the person, bottoms first and shoes last."""
        ordered = sorted(pieces, key=lambda piece: LAYER_ORDER.index(piece[1]))
        result = person
        async with httpx.AsyncClient(timeout=120) as client:
            for product, category in ordered:
                result = await self._dress(client, result, product)
                log.info("Vertex try-on dressed %s", category)
        return result
