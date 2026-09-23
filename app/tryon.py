import asyncio
import base64
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import Settings
from app.models import GalleryItem, GeminiUsageResponse, GeminiUsageSinceStart


ALLOWED_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class TryOnError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def validate_image(data: bytes, content_type: str | None, max_bytes: int) -> tuple[bytes, str, str]:
    if not data or len(data) > max_bytes:
        raise TryOnError(f"Image must be between 1 byte and {max_bytes // 1_000_000} MB", 400)
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            detected = Image.MIME.get(image.format or "")
    except (UnidentifiedImageError, OSError) as exc:
        raise TryOnError("The uploaded file is not a valid image", 400) from exc
    mime = detected if detected in ALLOWED_IMAGE_TYPES else content_type
    if mime not in ALLOWED_IMAGE_TYPES:
        raise TryOnError("Only JPEG, PNG, and WebP images are supported", 400)
    return data, mime, ALLOWED_IMAGE_TYPES[mime]


MODEL_IMAGE_MAX_SIDE = 1536
GEMINI_RETRY_MAX_SECONDS = 20


def _quota_details(response: httpx.Response) -> dict[str, Any]:
    """Read the QuotaFailure and RetryInfo details Google attaches to 429 responses."""
    quota: dict[str, Any] = {"limit_zero": False, "per_day": False, "retry_seconds": None, "model": None}
    try:
        details = response.json().get("error", {}).get("details") or []
    except (ValueError, AttributeError):
        return quota
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        kind = str(detail.get("@type", ""))
        if kind.endswith("QuotaFailure"):
            for violation in detail.get("violations") or []:
                if str(violation.get("quotaValue", "")).strip() == "0":
                    quota["limit_zero"] = True
                if "PerDay" in str(violation.get("quotaId", "")):
                    quota["per_day"] = True
                quota["model"] = (violation.get("quotaDimensions") or {}).get("model") or quota["model"]
        elif kind.endswith("RetryInfo"):
            try:
                quota["retry_seconds"] = float(str(detail.get("retryDelay", "")).removesuffix("s"))
            except ValueError:
                pass
    return quota


def _prepare_for_model(data: bytes, mime: str) -> tuple[bytes, str]:
    """Downscale large photos and apply EXIF rotation so requests stay well under Gemini's inline size limit."""
    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image)
        if max(image.size) <= MODEL_IMAGE_MAX_SIDE and len(data) <= 4_000_000:
            return data, mime
        image.thumbnail((MODEL_IMAGE_MAX_SIDE, MODEL_IMAGE_MAX_SIDE))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue(), "image/jpeg"


MAX_OUTFIT_PIECES = 5


@dataclass
class OutfitPiece:
    image: tuple[bytes, str, str]
    category: str
    label: str | None = None


@dataclass
class GeminiUsage:
    """Usage counted by this process. Resets when the server restarts."""
    since: datetime = field(default_factory=lambda: datetime.now(UTC))
    requests: int = 0
    succeeded: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    last_error: str | None = None


class TryOnService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._headers = {
            "apikey": settings.supabase_service_role_key.get_secret_value(),
            "Authorization": f"Bearer {settings.supabase_service_role_key.get_secret_value()}",
        }
        self.usage = GeminiUsage()

    def ensure_configured(self) -> None:
        missing = []
        if not self.settings.gemini_api_key.get_secret_value(): missing.append("GEMINI_API_KEY")
        if not self.settings.supabase_url: missing.append("SUPABASE_URL")
        if not self.settings.supabase_service_role_key.get_secret_value(): missing.append("SUPABASE_SERVICE_ROLE_KEY")
        if not self.settings.anonymous_token_secret.get_secret_value(): missing.append("ANONYMOUS_TOKEN_SECRET")
        if missing:
            raise TryOnError(f"Try-on service is not configured: {', '.join(missing)}", 503)

    async def fetch_image(self, url: str) -> tuple[bytes, str, str]:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(url, headers={"User-Agent": "FitCart/1.0"})
            response.raise_for_status()
        from app.security import validate_public_url
        validate_public_url(str(response.url))
        return validate_image(response.content, response.headers.get("content-type", "").split(";")[0], self.settings.max_image_bytes)

    async def generate(self, person: tuple[bytes, str, str], product: tuple[bytes, str, str], category: str) -> tuple[bytes, str, str]:
        return await self.generate_outfit(person, [OutfitPiece(image=product, category=category)])

    async def generate_outfit(self, person: tuple[bytes, str, str], pieces: list["OutfitPiece"]) -> tuple[bytes, str, str]:
        """Dress the person in one or more products (top, bottom, footwear, jewelry...) in a single image."""
        if not 1 <= len(pieces) <= MAX_OUTFIT_PIECES:
            raise TryOnError(f"Choose between 1 and {MAX_OUTFIT_PIECES} items to try on", 400)
        if len(pieces) == 1:
            prompt = (
                "Create a photorealistic virtual try-on using the first image as the person identity and body reference "
                "and the second image as the exact product reference. Put the product naturally on the person. "
                "Preserve the person's face, identity, skin tone, body proportions, pose, background, camera angle, and lighting. "
                f"The product category is {pieces[0].category}. Preserve its color, texture, print, logo, shape, and design. "
                "Do not alter unrelated clothing or add accessories. Return one full-body front-view image with no text or collage."
            )
        else:
            listing = "; ".join(
                f"image {index} is the {piece.category}" + (f" ({piece.label})" if piece.label else "")
                for index, piece in enumerate(pieces, start=2)
            )
            prompt = (
                "Create a photorealistic virtual try-on of a complete outfit. Image 1 is the person identity and body reference. "
                f"The other images are the exact product references: {listing}. "
                "Dress the person in every one of these products at the same time, replacing the clothing they currently wear in the same body areas. "
                "Preserve the person's face, identity, skin tone, body proportions, pose, background, camera angle, and lighting. "
                "Preserve each product's color, texture, print, logo, shape, and design exactly. Do not add items that were not provided. "
                "Return one full-body front-view image, head to feet, with no text or collage."
            )
        payload = {
            "contents": [{
                "role": "user",
                "parts": [{"text": prompt}, self._inline_part(person), *(self._inline_part(piece.image) for piece in pieces)],
            }],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": "3:4"},
            },
        }
        body = await self._call_gemini(self.settings.gemini_image_model, payload, "Gemini image generation failed")
        image = self._find_image(body)
        if not image:
            self.usage.failed += 1
            self.usage.last_error = "Gemini returned no generated image"
            raise TryOnError("Gemini returned no generated image")
        self.usage.succeeded += 1
        try:
            data = base64.b64decode(image["data"], validate=True)
        except (KeyError, ValueError) as exc:
            raise TryOnError("Gemini returned an invalid image") from exc
        return validate_image(data, image.get("mime_type") or image.get("mimeType") or "image/png", 20_000_000)

    async def generate_json(self, parts: list[dict[str, Any]], schema: dict[str, Any]) -> Any:
        """Ask the Gemini text model for JSON that matches ``schema``."""
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0.7},
        }
        body = await self._call_gemini(self.settings.gemini_text_model, payload, "Gemini outfit suggestions failed")
        texts = [
            part.get("text", "")
            for candidate in body.get("candidates") or []
            for part in ((candidate.get("content") or {}).get("parts") or [])
            if isinstance(part, dict) and not part.get("thought")
        ]
        try:
            result = json.loads("".join(texts))
        except ValueError as exc:
            self.usage.failed += 1
            raise TryOnError("Gemini returned an unreadable suggestion") from exc
        self.usage.succeeded += 1
        return result

    async def _call_gemini(self, model: str, payload: dict[str, Any], failure: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=180) as client:
            for attempt in range(2):
                self.usage.requests += 1
                response = await client.post(
                    f"{self._model_url(model)}:generateContent",
                    headers={"x-goog-api-key": self.settings.gemini_api_key.get_secret_value()},
                    json=payload,
                )
                if response.status_code != 429:
                    break
                quota = _quota_details(response)
                retry_in = quota["retry_seconds"]
                # A short per-minute limit clears by itself; wait it out once instead of failing the request.
                if attempt or quota["limit_zero"] or retry_in is None or retry_in > GEMINI_RETRY_MAX_SECONDS:
                    break
                self.usage.failed += 1
                await asyncio.sleep(retry_in)
        if response.status_code == 429:
            self.usage.failed += 1
            self.usage.last_error = f"429: {self._error_message(response)}"
            raise TryOnError(self._quota_message(_quota_details(response), model), 429)
        if response.status_code >= 400:
            self.usage.failed += 1
            self.usage.last_error = f"{response.status_code}: {self._error_message(response)}"
            raise TryOnError(f"{failure} ({self.usage.last_error})")
        body = response.json()
        self._record_tokens(body.get("usageMetadata") or {})
        return body

    def _quota_message(self, quota: dict[str, Any], model: str) -> str:
        model = quota["model"] or model
        if quota["limit_zero"]:
            return (
                f"The Gemini API key has no quota for {model}. Image generation is not included in the Gemini free tier; "
                "enable billing for the key's Google Cloud project in Google AI Studio, then try again."
            )
        if quota["per_day"]:
            return f"The daily Gemini quota for {model} is used up. It resets at midnight Pacific time, or raise the limit by enabling billing in Google AI Studio."
        if quota["retry_seconds"] is not None:
            return f"Gemini is rate limiting {model}. Please try again in about {max(1, round(quota['retry_seconds']))} seconds."
        return f"The Gemini quota for {model} is exhausted. Check usage and billing in Google AI Studio."

    def _model_url(self, model: str | None = None) -> str:
        model = quote((model or self.settings.gemini_image_model).removeprefix("models/"), safe="-._")
        return f"https://generativelanguage.googleapis.com/v1beta/models/{model}"

    def _record_tokens(self, metadata: dict[str, Any]) -> None:
        prompt = int(metadata.get("promptTokenCount") or 0)
        output = int(metadata.get("candidatesTokenCount") or 0)
        self.usage.prompt_tokens += prompt
        self.usage.output_tokens += output
        self.usage.total_tokens += int(metadata.get("totalTokenCount") or prompt + output)

    async def gemini_usage(self) -> GeminiUsageResponse:
        """Checks the key against the configured model and reports usage this server has recorded.

        Gemini API keys cannot read remaining quota or billing balance, so that is left to AI Studio.
        """
        key_valid = model_available = False
        message: str | None = None
        if not self.settings.gemini_api_key.get_secret_value():
            message = "GEMINI_API_KEY is not configured"
        else:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.get(self._model_url(), headers={"x-goog-api-key": self.settings.gemini_api_key.get_secret_value()})
                if response.status_code < 400:
                    key_valid = model_available = True
                else:
                    message = f"{response.status_code}: {self._error_message(response)}"
                    # 404 means the key was accepted but the model ID is unknown.
                    key_valid = response.status_code == 404
            except httpx.HTTPError as exc:
                message = f"Could not reach Gemini: {exc.__class__.__name__}"
        usage = self.usage
        return GeminiUsageResponse(
            model=self.settings.gemini_image_model,
            key_valid=key_valid,
            model_available=model_available,
            check_message=message,
            total_saved_tryons=await self._count_saved_tryons(),
            since_server_start=GeminiUsageSinceStart(
                since=usage.since, requests=usage.requests, succeeded=usage.succeeded, failed=usage.failed,
                prompt_tokens=usage.prompt_tokens, output_tokens=usage.output_tokens, total_tokens=usage.total_tokens,
                last_error=usage.last_error,
            ),
        )

    async def _count_saved_tryons(self) -> int | None:
        if not self.settings.supabase_url or not self.settings.supabase_service_role_key.get_secret_value():
            return None
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.head(
                    f"{self.settings.supabase_url.rstrip('/')}/rest/v1/try_on_gallery",
                    headers={**self._headers, "Prefer": "count=exact", "Range": "0-0"},
                    params={"select": "id"},
                )
        except httpx.HTTPError:
            return None
        total = response.headers.get("content-range", "").rpartition("/")[2]
        return int(total) if response.status_code < 400 and total.isdigit() else None

    @staticmethod
    def _inline_part(image: tuple[bytes, str, str]) -> dict[str, Any]:
        data, mime = _prepare_for_model(image[0], image[1])
        return {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            message = response.json().get("error", {}).get("message")
        except ValueError:
            message = None
        return (message or response.text or "unknown error").strip()[:300]

    def _find_image(self, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if isinstance(value.get("data"), str) and str(value.get("mime_type", "")).startswith("image/"):
                return value
            inline = value.get("inlineData") or value.get("inline_data")
            if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                return {"data": inline["data"], "mime_type": inline.get("mimeType") or inline.get("mime_type")}
            for key in ("candidates", "parts", "output_image", "outputs", "output", "content", "steps"):
                found = self._find_image(value.get(key)) if key in value else None
                if found: return found
        elif isinstance(value, list):
            for item in value:
                found = self._find_image(item)
                if found: return found
        return None

    async def _upload(self, path: str, image: tuple[bytes, str, str]) -> None:
        url = f"{self.settings.supabase_url.rstrip('/')}/storage/v1/object/{self.settings.supabase_storage_bucket}/{quote(path)}"
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, headers={**self._headers, "Content-Type": image[1], "x-upsert": "false"}, content=image[0])
        if response.status_code >= 400:
            raise TryOnError("Could not save image to the private gallery")

    async def _signed_url(self, path: str) -> str:
        base = self.settings.supabase_url.rstrip("/")
        url = f"{base}/storage/v1/object/sign/{self.settings.supabase_storage_bucket}/{quote(path)}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers={**self._headers, "Content-Type": "application/json"}, json={"expiresIn": self.settings.gallery_signed_url_seconds})
        if response.status_code >= 400:
            raise TryOnError("Could not create a private gallery URL")
        signed = response.json().get("signedURL") or response.json().get("signedUrl")
        if not signed: raise TryOnError("Supabase returned no signed URL")
        return signed if signed.startswith("http") else f"{base}/storage/v1{signed}"

    async def download(self, path: str) -> tuple[bytes, str, str]:
        url = f"{self.settings.supabase_url.rstrip('/')}/storage/v1/object/{self.settings.supabase_storage_bucket}/{quote(path)}"
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, headers=self._headers)
        if response.status_code >= 400:
            raise TryOnError("Could not read a saved wardrobe image")
        return validate_image(response.content, response.headers.get("content-type", "").split(";")[0], 20_000_000)

    async def delete_objects(self, paths: list[str]) -> None:
        if not paths:
            return
        url = f"{self.settings.supabase_url.rstrip('/')}/storage/v1/object/{self.settings.supabase_storage_bucket}"
        async with httpx.AsyncClient(timeout=30) as client:
            await client.request("DELETE", url, headers={**self._headers, "Content-Type": "application/json"}, json={"prefixes": paths})

    async def signed_urls(self, paths: list[str]) -> dict[str, str]:
        """Sign many private objects in one request."""
        if not paths:
            return {}
        base = self.settings.supabase_url.rstrip("/")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{base}/storage/v1/object/sign/{self.settings.supabase_storage_bucket}",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"expiresIn": self.settings.gallery_signed_url_seconds, "paths": paths},
            )
        if response.status_code >= 400:
            raise TryOnError("Could not create private wardrobe URLs")
        signed: dict[str, str] = {}
        for entry in response.json():
            link = entry.get("signedURL") or entry.get("signedUrl")
            if entry.get("path") and link:
                signed[entry["path"]] = link if link.startswith("http") else f"{base}/storage/v1{link}"
        return signed

    async def rest(self, method: str, table: str, *, params: dict[str, str] | None = None, json_body: Any = None, prefer: str | None = None) -> httpx.Response:
        headers = {**self._headers, "Content-Type": "application/json"}
        if prefer:
            headers["Prefer"] = prefer
        async with httpx.AsyncClient(timeout=30) as client:
            return await client.request(method, f"{self.settings.supabase_url.rstrip('/')}/rest/v1/{table}", headers=headers, params=params, json=json_body)

    async def upload(self, path: str, image: tuple[bytes, str, str]) -> None:
        await self._upload(path, image)

    async def save(self, user_id: str, person: tuple[bytes, str, str], product: tuple[bytes, str, str], result: tuple[bytes, str, str], category: str, product_source: str, product_url: str | None, items: list[dict[str, Any]] | None = None) -> GalleryItem:
        item_id = str(uuid4())
        prefix = f"{user_id}/{item_id}"
        paths = {"person": f"{prefix}/person.{person[2]}", "product": f"{prefix}/product.{product[2]}", "result": f"{prefix}/result.{result[2]}"}
        await self._upload(paths["person"], person)
        await self._upload(paths["product"], product)
        await self._upload(paths["result"], result)
        row = {"id": item_id, "anonymous_user_id": user_id, "category": category, "product_source": product_source, "product_url": product_url, "person_path": paths["person"], "product_path": paths["product"], "result_path": paths["result"], "model": self.settings.gemini_image_model}
        if items:
            row["items"] = items
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.settings.supabase_url.rstrip('/')}/rest/v1/try_on_gallery", headers={**self._headers, "Content-Type": "application/json", "Prefer": "return=representation"}, json=row)
        if response.status_code >= 400: raise TryOnError("Could not save the gallery record")
        created = response.json()[0]
        return await self._to_item(created)

    async def list_gallery(self, user_id: str) -> list[GalleryItem]:
        params = {"anonymous_user_id": f"eq.{user_id}", "select": "*", "order": "created_at.desc"}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.settings.supabase_url.rstrip('/')}/rest/v1/try_on_gallery", headers=self._headers, params=params)
        if response.status_code >= 400: raise TryOnError("Could not load the gallery")
        return [await self._to_item(row) for row in response.json()]

    async def _to_item(self, row: dict[str, Any]) -> GalleryItem:
        return GalleryItem(id=row["id"], anonymous_user_id=row["anonymous_user_id"], category=row["category"], product_source=row["product_source"], product_url=row.get("product_url"), person_image_url=await self._signed_url(row["person_path"]), product_image_url=await self._signed_url(row["product_path"]), result_image_url=await self._signed_url(row["result_path"]), model=row["model"], items=row.get("items") or [], created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")))
