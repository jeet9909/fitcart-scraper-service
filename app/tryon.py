import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import base64
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from app import activity, identity
from app.config import Settings
from app.models import GalleryItem, GeminiUsageResponse, GeminiUsageSinceStart, PoseImage
from app.vertex_tryon import VertexTryOn, VertexTryOnError

log = logging.getLogger(__name__)


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


MODEL_IMAGE_MAX_SIDE = 1024
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
        if max(image.size) <= MODEL_IMAGE_MAX_SIDE and len(data) <= 1_500_000:
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


POSES = ("standard", "keep")


def _describe_pieces(pieces: list["OutfitPiece"], first: int = 2) -> str:
    return "; ".join(
        f"image {index} is the {piece.category}" + (f" ({piece.label})" if piece.label else "")
        for index, piece in enumerate(pieces, start=first)
    )


# Glasses drawn twice (one pair on top of another) were the most visible glitch in re-posed results.
FACE_ACCESSORIES = (
    "Keep every accessory on their face and head exactly as in their photo, and add none they do not wear: "
    "glasses or sunglasses with the same frame style (thick or thin, full-rim or metal), shape, colour, thickness and lens tint, "
    "sitting on the nose and ears in the same place, "
    "and earrings, nose pins, piercings, bindi, caps or headwear the same way. "
    "Glasses appear as one single pair with clean, sharp frames: no double, overlapping, ghosted or broken frames or lenses. "
)


def tryon_prompt(pieces: list["OutfitPiece"], pose: str = "standard", face_reference: bool = False) -> str:
    """Instruction for the image model. Image 1 is the person, then an optional face close-up, then the products in order."""
    first_product = 3 if face_reference else 2
    areas = ", ".join(dict.fromkeys(piece.category for piece in pieces))
    # Identity comes first: the model weighs early instructions most.
    identity = (
        "This is the same real person, not a model or a lookalike. "
        + ("Image 2 is a close-up of their face: treat it as the identity reference and reproduce that exact face. " if face_reference else "")
        + "Keep their exact face: eye shape and spacing, eyebrows, nose, lips, jawline, face width, skin texture, moles and marks, "
        "hairstyle and hairline, facial hair, skin tone and age. Do not beautify, smooth, slim or idealise anything. "
        + FACE_ACCESSORIES
        + "Keep their real body: the same height, head size relative to the body, shoulder width, chest, waist, hips, arm and leg length and overall build. "
    )
    products = (
        f"Image 1 shows the person. The other images are the exact product references: {_describe_pieces(pieces, first_product)}. "
        "Product photos may show a model wearing other clothes or accessories; take only the listed product from each photo and ignore everything else. "
        f"Dress the person in {'this product' if len(pieces) == 1 else 'all of these products at the same time'}, replacing what they wear in the {areas} area. "
        "Reproduce each product exactly: same color, fabric texture, print, pattern, logo, collar, sleeves, length, fit and design details. "
        "Do not add clothing, jewelry or accessories that were not provided; the person's own glasses and face accessories stay as they are. "
    )
    quality = "Photorealistic, natural fabric folds and fit, anatomically correct hands with five fingers each. One single person, no text, no watermark, no collage, no borders."
    if pose == "keep":
        return (
            "Photorealistic virtual try-on of the person in image 1. " + identity + products
            + "Keep the person's own pose, background, camera angle and lighting from image 1, and keep their own clothing in body areas the products do not cover. "
            + "Show the full body from head to feet if image 1 does. " + quality
        )
    return (
        "Photorealistic full-body fashion catalogue photo of the person in image 1 wearing the products. " + identity
        + "Only the position of the arms, legs and head changes; body size, proportions and the face stay exactly as in image 1. " + products
        + "Pose: ignore the pose in image 1 and stand the person upright, facing the camera straight on, weight evenly on both feet, feet slightly apart, "
        + "arms relaxed and straight down at the sides and held slightly away from the torso so the arms and hands cover no part of the outfit, "
        + "hands open and relaxed, shoulders level, head straight and facing the camera like in a passport photo, calm neutral expression, looking at the camera. "
        + "Framing: vertical portrait with the entire body in frame from the top of the head to the soles of the shoes, a small margin above the head and below the feet, "
        + "camera at chest height with no tilt, nothing cropped. "
        + "Background and light: a plain, light neutral studio backdrop with soft, even front lighting so every garment is clearly visible. "
        + "For body areas the products do not cover, keep the person's own clothing from image 1; if those areas are not visible in image 1, "
        + "use simple plain neutral items that suit the outfit (for example plain trousers or plain shoes). " + quality
    )


FACE_REFINE_PROMPT = (
    "Edit image 1. Images 2 and 3 show the real person: image 2 is a close-up of their face, image 3 is their own photo. "
    "Replace the whole head in image 1 so it is exactly this real person, copied from images 2 and 3: the same face shape and width, cheeks, jawline and chin, "
    "beard and moustache shape, density and length (not trimmed, thinned or filled in), eyes, eyebrows, nose, lips, skin tone and texture, "
    "ears, hairline, and hairstyle with the same volume and height. The head must be the same size relative to the shoulders and body "
    "as in image 3: a real person's head is about one seventh of their height, so never shrink it to fashion-model proportions. "
    "Do not slim, smooth, beautify or idealise the face. "
    + FACE_ACCESSORIES
    + "First remove any glasses or face accessories already drawn in image 1, then draw only the ones from images 2 and 3, once, in their real position. "
    "Keep everything else in image 1 exactly as it is: the pose, body, clothes, hands, background, lighting, camera framing and image size. "
    "Photorealistic, one person, no text."
)


# 360° view: the finished look is redrawn from these turns around the person (degrees, clockwise seen
# from above; 0 is the saved front-facing image). The viewer spins through front, right, back, left.
SPIN_ANGLES = (90, 180, 270)
SPIN_VIEWS = {
    90: "turned 90 degrees to their left, so the camera sees their right side in full profile",
    180: "turned around 180 degrees, so the camera sees them directly from behind: the back of the head and hair, "
         "the back of every garment and the heels of the shoes. The face is not visible",
    270: "turned 90 degrees to their right, so the camera sees their left side in full profile",
}


# Social-ready poses: the finished look redrawn in a pose and setting made for a feed post (4:5).
# Plus gets the three that read best as posts; Pro gets all of them. Keys are shared with the web app.
@dataclass(frozen=True)
class SocialPose:
    key: str
    label: str
    plan: str  # lowest plan that includes it: "plus" or "pro"
    pose: str
    setting: str


SOCIAL_POSES: dict[str, SocialPose] = {pose.key: pose for pose in (
    SocialPose("street-walk", "Street walk", "plus",
               "walking towards the camera mid-stride, one foot forward, arms swinging naturally, relaxed confident expression",
               "a clean, softly lit city street with blurred shopfronts behind"),
    SocialPose("mirror-selfie", "Mirror selfie", "plus",
               "taking a full-length mirror selfie holding a plain phone at chest height, the phone not covering the face",
               "a tidy, bright bedroom or dressing area with a tall mirror"),
    SocialPose("over-shoulder", "Over the shoulder", "plus",
               "body turned three-quarters away, looking back over the shoulder at the camera",
               "a bright minimal studio with a soft beige backdrop"),
    SocialPose("pockets", "Hands in pockets", "pro",
               "standing relaxed with weight on one leg, hands in pockets or resting on the hips if the outfit has no pockets, slight smile",
               "a plain warm-toned wall with soft daylight and a gentle shadow"),
    SocialPose("wall-lean", "Wall lean", "pro",
               "leaning one shoulder against a wall, legs crossed at the ankles, arms relaxed",
               "a textured light concrete wall in late-afternoon sun"),
    SocialPose("seated", "Seated", "pro",
               "sitting on a simple stool or low steps with both feet near the body, hands resting naturally, the camera at eye level so the "
               "legs and feet keep natural proportions",
               "a calm, softly lit interior with neutral tones"),
    SocialPose("candid-laugh", "Candid laugh", "pro",
               "a genuine candid laugh with a wide natural smile, glancing slightly away from the camera, relaxed shoulders, mid-movement",
               "an outdoor terrace with soft golden-hour light and a blurred background"),
    SocialPose("power-stance", "Power stance", "pro",
               "a bold editorial stance: feet planted wide, one hand on the hip, shoulders squared, chin slightly raised, a strong gaze into "
               "the camera, photographed from a slightly low angle",
               "a solid-colour studio backdrop that complements the outfit"),
)}


def social_pose_prompt(pose: SocialPose) -> str:
    return (
        "Image 1 is a finished fashion photo of a person. Create a new photorealistic photo for a social media post of exactly this person "
        "wearing exactly this outfit: the same face, hairstyle, skin tone, body size and proportions, and every garment, colour, print, fabric, "
        "fit, length and the shoes unchanged. Do not add or remove any garment detail such as pockets, buttons, seams, logos or prints. "
        "Image 2 is their own photo, for their real face and build; image 3 is a product reference. "
        + FACE_ACCESSORIES
        + f"Pose: {pose.pose}. Setting: {pose.setting}. "
        "Vertical 4:5 framing like a fashion influencer post, showing the whole body from the top of the head to the shoes with a little space "
        "around, so the complete outfit including the footwear is visible; nothing cropped. Natural flattering light, sharp focus on the person, "
        "shallow depth of field. Anatomically correct hands with five fingers each. One person, no text, no logo, no watermark, no borders, no collage."
    )


def spin_prompt(angle: int) -> str:
    return (
        "Image 1 is a finished fashion photo of a person. Redraw the same photo with the person " + SPIN_VIEWS[angle] + ". "
        "It is the same moment from a camera that walked around them: the same person, body size and proportions, height, "
        "hairstyle, skin tone, glasses and accessories, and exactly the same outfit, colours, prints, fabric, fit, length and shoes. "
        "Image 2 is the person's own photo and image 3 is a product reference; use them for details that image 1 does not show, "
        "such as the back of a garment, but never change what image 1 already shows. "
        "Keep the same plain studio background, the same soft even lighting, the same camera height and distance, and the same framing: "
        "the whole body from the top of the head to the soles of the shoes, the same size in the frame as in image 1, standing upright "
        "with arms relaxed at the sides. Photorealistic, one person, no text, no watermark, no collage."
    )


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
        self.vertex = VertexTryOn(settings)
        self._pool: tuple[asyncio.AbstractEventLoop, httpx.AsyncClient] | None = None

    @asynccontextmanager
    async def _supabase(self) -> AsyncIterator[httpx.AsyncClient]:
        """One pooled client per event loop: Supabase calls reuse warm TLS connections instead of reconnecting."""
        loop = asyncio.get_running_loop()
        if self._pool is None or self._pool[0] is not loop or self._pool[1].is_closed:
            self._pool = (loop, httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_connections=20, max_keepalive_connections=10)))
        yield self._pool[1]

    async def _sb(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """A Supabase request on the pooled client. A pooled connection the server already closed fails
        with a transport error on first use, so that is retried once on a fresh client."""
        for attempt in range(2):
            try:
                async with self._supabase() as client:
                    return await client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt:
                    log.warning("Supabase %s failed twice: %r", method, exc)
                    raise TryOnError("Could not reach the storage service. Please try again.", 502) from exc
                log.info("Supabase %s failed (%r), retrying on a new connection", method, exc)
                if self._pool:
                    await self._pool[1].aclose()
                self._pool = None
        raise AssertionError("unreachable")

    def ensure_configured(self) -> None:
        missing = []
        if not self.settings.gemini_api_key.get_secret_value(): missing.append("GEMINI_API_KEY")
        if not self.settings.supabase_url: missing.append("SUPABASE_URL")
        if not self.settings.supabase_service_role_key.get_secret_value(): missing.append("SUPABASE_SERVICE_ROLE_KEY")
        if not self.settings.anonymous_token_secret.get_secret_value(): missing.append("ANONYMOUS_TOKEN_SECRET")
        if missing:
            raise TryOnError(f"Try-on service is not configured: {', '.join(missing)}", 503)

    async def fetch_image(self, url: str) -> tuple[bytes, str, str]:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                response = await client.get(url, headers={"User-Agent": "FitCart/1.0"})
                response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("Product image download failed for %s: %r", url[:200], exc)
            raise TryOnError("Could not download the product image. Try another photo of it, or upload it.", 502) from exc
        from app.security import validate_public_url
        validate_public_url(str(response.url))
        return validate_image(response.content, response.headers.get("content-type", "").split(";")[0], self.settings.max_image_bytes)

    async def generate(self, person: tuple[bytes, str, str], product: tuple[bytes, str, str], category: str, product_name: str | None = None, pose: str = "standard", face_check: bool = False) -> tuple[bytes, str, str]:
        return await self.generate_outfit(person, [OutfitPiece(image=product, category=category, label=product_name)], pose=pose, face_check=face_check)

    async def generate_outfit(self, person: tuple[bytes, str, str], pieces: list["OutfitPiece"], pose: str = "standard", face_check: bool = False) -> tuple[bytes, str, str]:
        """Dress the person in one or more products (top, bottom, footwear, jewelry...) in a single image.
        face_check (Plus and Pro) scores the refined face against the real photo and redraws weak matches."""
        if not 1 <= len(pieces) <= MAX_OUTFIT_PIECES:
            raise TryOnError(f"Choose between 1 and {MAX_OUTFIT_PIECES} items to try on", 400)
        if pose not in POSES:
            raise TryOnError(f"pose must be one of: {', '.join(POSES)}", 400)
        if pose == "keep" and self.vertex.configured and self.vertex.supports([piece.category for piece in pieces]):
            # Own pose: Google's try-on model repaints only the clothes, so body and head size stay exact.
            try:
                return await self.vertex.dress_outfit(person, [(piece.image, piece.category) for piece in pieces])
            except (VertexTryOnError, httpx.HTTPError) as exc:
                log.warning("Vertex try-on failed, using Gemini instead: %s", exc)
        face = await asyncio.to_thread(identity.face_reference, person[0]) if self.settings.face_lock_enabled else None
        prompt = tryon_prompt(pieces, pose, face_reference=face is not None)
        face_part = [self._inline_part((face[0], face[1], "jpg"))] if face else []
        payload = {
            "contents": [{
                "role": "user",
                "parts": [{"text": prompt}, self._inline_part(person), *face_part, *(self._inline_part(piece.image) for piece in pieces)],
            }],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": "3:4"},
            },
        }
        result = await self._generated_image(payload, "Gemini image generation failed")
        if pose == "standard" and face and self.settings.face_refine_enabled:
            # Re-posing redraws the whole person, so faces drift (often slimmer, a smaller head). A second,
            # edit-only pass fixes just the head against the real references; everything else stays.
            result = await self._refine_face(result, person, face, "3:4", "standard pose", check=face_check)
        elif pose == "keep" and self.settings.face_lock_enabled:
            # In the person's own pose the head barely moves, so pasting their real features is safe.
            locked = await asyncio.to_thread(identity.lock_face, person[0], result[0], result[1])
            if locked:
                result = (locked, result[1], result[2])
        return result

    async def _refine_face(self, image: tuple[bytes, str, str], person: tuple[bytes, str, str], face: tuple[bytes, str], aspect: str, label: str, check: bool = True) -> tuple[bytes, str, str]:
        """Edit-only pass that redraws the head from the real references. With check (Plus and Pro), the
        result is scored with face recognition against the real photo; below the target it is redrawn again
        and the closest version wins (the unrefined image included). Without check, or without a readable
        face, the refined image is used as it is."""
        refine = {"contents": [{"role": "user", "parts": [
            {"text": FACE_REFINE_PROMPT}, self._inline_part(image), self._inline_part((face[0], face[1], "jpg")), self._inline_part(person),
        ]}], "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": aspect}}}
        if not check:
            try:
                refined = await self._generated_image(refine, "Gemini face refinement failed")
                log.info("Face refinement for %s applied (no identity check on this plan)", label)
                return refined
            except TryOnError as exc:
                log.warning("Face refinement skipped for %s: %s", label, exc)
                return image
        real = await asyncio.to_thread(identity.face_signature, person[0])
        before = await asyncio.to_thread(identity.face_match, real, image[0])
        best, best_score, scores = image, before, [before]
        for attempt in range(1 + self.settings.face_refine_retries):
            try:
                refined = await self._generated_image(refine, "Gemini face refinement failed")
            except TryOnError as exc:
                log.warning("Face refinement attempt %d skipped for %s: %s", attempt + 1, label, exc)
                break
            score = await asyncio.to_thread(identity.face_match, real, refined[0])
            scores.append(score)
            if score is None or best_score is None or score >= best_score:
                best, best_score = refined, score
            if score is None or score >= self.settings.face_match_target:
                break
        log.info("Face refinement for %s: scores %s, kept %s", label,
                 ", ".join("n/a" if x is None else f"{x:.3f}" for x in scores), "n/a" if best_score is None else f"{best_score:.3f}")
        return best

    async def generate_spin_view(self, look: tuple[bytes, str, str], person: tuple[bytes, str, str], product: tuple[bytes, str, str], angle: int) -> tuple[bytes, str, str]:
        """Draw one side or back view of a finished look, for the 360° viewer. Retries once, since one bad view breaks the spin."""
        payload = {
            "contents": [{"role": "user", "parts": [
                {"text": spin_prompt(angle)}, self._inline_part(look), self._inline_part(person), self._inline_part(product),
            ]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": "3:4"}},
        }
        try:
            return await self._generated_image(payload, "Gemini 360 view failed")
        except TryOnError as exc:
            if exc.status_code == 429:
                raise
            log.warning("360 view %s failed once, retrying: %s", angle, exc)
            return await self._generated_image(payload, "Gemini 360 view failed")

    async def create_social_pose(self, user_id: str, row: dict[str, Any], pose: SocialPose) -> GalleryItem:
        """Draw a saved look in a social pose, then fix the face against the real photo, and store it."""
        look, person, product = await asyncio.gather(
            self.download(row["result_path"]), self.download(row["person_path"]), self.download(row["product_path"]))
        config = {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": "4:5"}}
        payload = {"contents": [{"role": "user", "parts": [
            {"text": social_pose_prompt(pose)}, self._inline_part(look), self._inline_part(person), self._inline_part(product),
        ]}], "generationConfig": config}
        image = await self._generated_image(payload, "Gemini social pose failed")
        face = await asyncio.to_thread(identity.face_reference, person[0]) if self.settings.face_lock_enabled else None
        if face and self.settings.face_refine_enabled:
            # A new pose redraws the whole person, so the face drifts the same way as in the standard pose.
            image = await self._refine_face(image, person, face, "4:5", f"pose {pose.key}", check=True)
        path = f"{user_id}/{row['id']}/pose_{pose.key}_{uuid4().hex[:8]}.{image[2]}"
        await self._upload(path, image)
        shots = [shot for shot in row.get("pose_shots") or [] if shot.get("pose") != pose.key] + [{"pose": pose.key, "path": path}]
        response = await self.rest(
            "PATCH", "try_on_gallery", params={"id": f"eq.{row['id']}", "anonymous_user_id": f"eq.{user_id}"},
            json_body={"pose_shots": shots}, prefer="return=representation")
        if response.status_code >= 400:
            await self.delete_objects([path])
            log.error("Could not save the social pose: %s %s", response.status_code, response.text[:300])
            raise TryOnError("Could not save the pose. Run supabase/schema.sql to add the pose_shots column.", 503)
        log.info("Social pose %s created for look %s", pose.key, row["id"])
        return await self._to_item(response.json()[0])

    async def get_gallery_row(self, user_id: str, item_id: str) -> dict[str, Any]:
        response = await self.rest("GET", "try_on_gallery", params={"id": f"eq.{item_id}", "anonymous_user_id": f"eq.{user_id}", "select": "*"})
        if response.status_code >= 400:
            raise TryOnError("Could not load this look")
        rows = response.json()
        if not rows:
            raise TryOnError("This look was not found", 404)
        return rows[0]

    async def create_spin(self, user_id: str, row: dict[str, Any]) -> GalleryItem:
        """Draw the right, back and left views of a saved look and store them next to it."""
        look, person, product = await asyncio.gather(
            self.download(row["result_path"]), self.download(row["person_path"]), self.download(row["product_path"]))
        views = await asyncio.gather(*(self.generate_spin_view(look, person, product, angle) for angle in SPIN_ANGLES))
        prefix = f"{user_id}/{row['id']}"
        paths = [f"{prefix}/spin_{angle}_{uuid4().hex[:8]}.{view[2]}" for angle, view in zip(SPIN_ANGLES, views)]
        await asyncio.gather(*(self._upload(path, view) for path, view in zip(paths, views)))
        spin = [row["result_path"], *paths]
        response = await self.rest(
            "PATCH", "try_on_gallery", params={"id": f"eq.{row['id']}", "anonymous_user_id": f"eq.{user_id}"},
            json_body={"spin_paths": spin}, prefer="return=representation")
        if response.status_code >= 400:
            await self.delete_objects(paths)
            log.error("Could not save the 360 view: %s %s", response.status_code, response.text[:300])
            raise TryOnError("Could not save the 360° view. Run supabase/schema.sql to add the spin_paths column.", 503)
        log.info("360 view created for look %s", row["id"])
        return await self._to_item(response.json()[0])

    async def _generated_image(self, payload: dict[str, Any], failure: str) -> tuple[bytes, str, str]:
        body = await self._call_gemini(self.settings.gemini_image_model, payload, failure)
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
                activity.count_call("gemini_image" if model == self.settings.gemini_image_model else "gemini_text")
                try:
                    response = await client.post(
                        f"{self._model_url(model)}:generateContent",
                        headers={"x-goog-api-key": self.settings.gemini_api_key.get_secret_value()},
                        json=payload,
                    )
                except httpx.TimeoutException as exc:
                    self.usage.failed += 1
                    self.usage.last_error = f"timeout: {exc!r}"
                    log.warning("%s: Gemini timed out", failure)
                    raise TryOnError("The image model took too long to answer. Please try again.", 504) from exc
                except httpx.HTTPError as exc:
                    self.usage.failed += 1
                    self.usage.last_error = f"network: {exc!r}"
                    if attempt:
                        log.warning("%s: Gemini unreachable: %r", failure, exc)
                        raise TryOnError("Could not reach the image model. Please try again.", 502) from exc
                    log.info("%s: Gemini connection failed (%r), retrying", failure, exc)
                    continue
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
            async with self._supabase() as client:
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
        """The final generated image. Gemini 3 image models first return up to two draft images marked
        thought=true; the final image is the last part without that flag."""
        images: list[tuple[bool, dict[str, Any]]] = []
        self._collect_images(value, images, thought=False)
        final = [image for is_thought, image in images if not is_thought]
        if final:
            return final[-1]
        return images[-1][1] if images else None

    def _collect_images(self, value: Any, found: list[tuple[bool, dict[str, Any]]], thought: bool) -> None:
        if isinstance(value, dict):
            thought = thought or bool(value.get("thought"))
            if isinstance(value.get("data"), str) and str(value.get("mime_type", "")).startswith("image/"):
                found.append((thought, value))
                return
            inline = value.get("inlineData") or value.get("inline_data")
            if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                found.append((thought, {"data": inline["data"], "mime_type": inline.get("mimeType") or inline.get("mime_type")}))
                return
            for key in ("candidates", "parts", "output_image", "outputs", "output", "content", "steps"):
                if key in value:
                    self._collect_images(value[key], found, thought)
        elif isinstance(value, list):
            for item in value:
                self._collect_images(item, found, thought)

    async def _upload(self, path: str, image: tuple[bytes, str, str]) -> None:
        url = f"{self.settings.supabase_url.rstrip('/')}/storage/v1/object/{self.settings.supabase_storage_bucket}/{quote(path)}"
        # Paths are unique, so upsert only matters when a retried upload had in fact already landed.
        response = await self._sb("POST", url, headers={**self._headers, "Content-Type": image[1], "x-upsert": "true"}, content=image[0])
        if response.status_code >= 400:
            raise TryOnError("Could not save image to the private gallery")

    async def download(self, path: str) -> tuple[bytes, str, str]:
        url = f"{self.settings.supabase_url.rstrip('/')}/storage/v1/object/{self.settings.supabase_storage_bucket}/{quote(path)}"
        response = await self._sb("GET", url, headers=self._headers)
        if response.status_code >= 400:
            raise TryOnError("Could not read a saved wardrobe image")
        return validate_image(response.content, response.headers.get("content-type", "").split(";")[0], 20_000_000)

    async def delete_objects(self, paths: list[str]) -> None:
        if not paths:
            return
        url = f"{self.settings.supabase_url.rstrip('/')}/storage/v1/object/{self.settings.supabase_storage_bucket}"
        await self._sb("DELETE", url, headers={**self._headers, "Content-Type": "application/json"}, json={"prefixes": paths})

    async def signed_urls(self, paths: list[str]) -> dict[str, str]:
        """Sign many private objects in one request."""
        if not paths:
            return {}
        base = self.settings.supabase_url.rstrip("/")
        response = await self._sb(
            "POST", f"{base}/storage/v1/object/sign/{self.settings.supabase_storage_bucket}",
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
        return await self._sb(method, f"{self.settings.supabase_url.rstrip('/')}/rest/v1/{table}", headers=headers, params=params, json=json_body)

    async def upload(self, path: str, image: tuple[bytes, str, str]) -> None:
        await self._upload(path, image)

    async def save(self, user_id: str, person: tuple[bytes, str, str], product: tuple[bytes, str, str], result: tuple[bytes, str, str], category: str, product_source: str, product_url: str | None, items: list[dict[str, Any]] | None = None) -> GalleryItem:
        item_id = str(uuid4())
        prefix = f"{user_id}/{item_id}"
        paths = {"person": f"{prefix}/person.{person[2]}", "product": f"{prefix}/product.{product[2]}", "result": f"{prefix}/result.{result[2]}"}
        await asyncio.gather(self._upload(paths["person"], person), self._upload(paths["product"], product), self._upload(paths["result"], result))
        row = {"id": item_id, "anonymous_user_id": user_id, "category": category, "product_source": product_source, "product_url": product_url, "person_path": paths["person"], "product_path": paths["product"], "result_path": paths["result"], "model": self.settings.gemini_image_model}
        if items:
            row["items"] = items
        response = await self._sb("POST", f"{self.settings.supabase_url.rstrip('/')}/rest/v1/try_on_gallery", headers={**self._headers, "Content-Type": "application/json", "Prefer": "return=representation"}, json=row)
        if response.status_code >= 400: raise TryOnError("Could not save the gallery record")
        created = response.json()[0]
        return await self._to_item(created)

    async def list_gallery(self, user_id: str) -> list[GalleryItem]:
        params = {"anonymous_user_id": f"eq.{user_id}", "select": "*", "order": "created_at.desc"}
        response = await self._sb("GET", f"{self.settings.supabase_url.rstrip('/')}/rest/v1/try_on_gallery", headers=self._headers, params=params)
        if response.status_code >= 400: raise TryOnError("Could not load the gallery")
        rows = response.json()
        signed = await self.signed_urls([path for row in rows for path in _row_paths(row)])
        return [await self._to_item(row, signed) for row in rows]

    async def _to_item(self, row: dict[str, Any], signed: dict[str, str] | None = None) -> GalleryItem:
        if signed is None:
            signed = await self.signed_urls(_row_paths(row))
        missing = [path for path in _row_paths(row) if path not in signed]
        if missing:
            raise TryOnError("Could not create a private gallery URL")
        spin = [signed[path] for path in row.get("spin_paths") or []]
        poses = [PoseImage(pose=shot["pose"], label=SOCIAL_POSES[shot["pose"]].label if shot["pose"] in SOCIAL_POSES else shot["pose"], url=signed[shot["path"]])
                 for shot in row.get("pose_shots") or []]
        return GalleryItem(id=row["id"], anonymous_user_id=row["anonymous_user_id"], category=row["category"], product_source=row["product_source"], product_url=row.get("product_url"), person_image_url=signed[row["person_path"]], product_image_url=signed[row["product_path"]], result_image_url=signed[row["result_path"]], model=row["model"], items=row.get("items") or [], spin_image_urls=spin, pose_images=poses, created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")))


def _row_paths(row: dict[str, Any]) -> list[str]:
    return [row["person_path"], row["product_path"], row["result_path"], *(row.get("spin_paths") or []),
            *(shot["path"] for shot in row.get("pose_shots") or [])]
