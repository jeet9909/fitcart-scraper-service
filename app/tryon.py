import base64
import io
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import Settings
from app.models import GalleryItem


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


class TryOnService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._headers = {
            "apikey": settings.supabase_service_role_key.get_secret_value(),
            "Authorization": f"Bearer {settings.supabase_service_role_key.get_secret_value()}",
        }

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
        prompt = (
            "Create a photorealistic virtual try-on using the first image as the person identity and body reference "
            "and the second image as the exact product reference. Put the product naturally on the person. "
            "Preserve the person's face, identity, skin tone, body proportions, pose, background, camera angle, and lighting. "
            f"The product category is {category}. Preserve its color, texture, print, logo, shape, and design. "
            "Do not alter unrelated clothing or add accessories. Return one full-body front-view image with no text or collage."
        )
        payload = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": prompt},
                    self._inline_part(person),
                    self._inline_part(product),
                ],
            }],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": "3:4"},
            },
        }
        model = quote(self.settings.gemini_image_model.removeprefix("models/"), safe="-._")
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": self.settings.gemini_api_key.get_secret_value()},
                json=payload,
            )
        if response.status_code >= 400:
            raise TryOnError(f"Gemini image generation failed ({response.status_code}): {self._error_message(response)}")
        image = self._find_image(response.json())
        if not image:
            raise TryOnError("Gemini returned no generated image")
        try:
            data = base64.b64decode(image["data"], validate=True)
        except (KeyError, ValueError) as exc:
            raise TryOnError("Gemini returned an invalid image") from exc
        return validate_image(data, image.get("mime_type") or image.get("mimeType") or "image/png", 20_000_000)

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

    async def save(self, user_id: str, person: tuple[bytes, str, str], product: tuple[bytes, str, str], result: tuple[bytes, str, str], category: str, product_source: str, product_url: str | None) -> GalleryItem:
        item_id = str(uuid4())
        prefix = f"{user_id}/{item_id}"
        paths = {"person": f"{prefix}/person.{person[2]}", "product": f"{prefix}/product.{product[2]}", "result": f"{prefix}/result.{result[2]}"}
        await self._upload(paths["person"], person)
        await self._upload(paths["product"], product)
        await self._upload(paths["result"], result)
        row = {"id": item_id, "anonymous_user_id": user_id, "category": category, "product_source": product_source, "product_url": product_url, "person_path": paths["person"], "product_path": paths["product"], "result_path": paths["result"], "model": self.settings.gemini_image_model}
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
        return GalleryItem(id=row["id"], anonymous_user_id=row["anonymous_user_id"], category=row["category"], product_source=row["product_source"], product_url=row.get("product_url"), person_image_url=await self._signed_url(row["person_path"]), product_image_url=await self._signed_url(row["product_path"]), result_image_url=await self._signed_url(row["result_path"]), model=row["model"], created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")))
