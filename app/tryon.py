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
from app.admin_store import BACKED_UP_TABLES, BackupTables
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
GENERATED_IMAGE_MAX_BYTES = 80_000_000  # a 4K PNG from Gemini can be 20 to 40 MB before it is stored as JPEG
# Paid looks end with one pass that returns the finished photo at full resolution. It runs after the face,
# skin-tone and size fixes, which work on the normal-size image (on a 4K image they would need too much memory).
UPSCALE_PROMPT = (
    "Image 1 is a finished fashion photo. Return exactly the same photo at high resolution: the identical person, face, "
    "eyes, hair, skin tone, glasses, body, pose, size and position in the frame, the identical outfit, colours, prints, "
    "fabric, fit, sleeve length and shoes, and the identical background, framing and lighting. Only add true fine detail "
    "such as fabric weave, stitching, hair strands and natural skin texture. Do not change, move, add or remove anything. "
    "Photorealistic, one person, no text, no watermark, no borders."
)
ASPECT_RATIOS = {"1:1": 1.0, "2:3": 2 / 3, "3:2": 1.5, "3:4": 0.75, "4:3": 4 / 3, "4:5": 0.8, "5:4": 1.25, "9:16": 9 / 16, "16:9": 16 / 9}


def _aspect_of(image: tuple[bytes, str, str]) -> str:
    with Image.open(io.BytesIO(image[0])) as picture:
        ratio = picture.width / max(picture.height, 1)
    return min(ASPECT_RATIOS, key=lambda name: abs(ASPECT_RATIOS[name] - ratio))


FULL_RES_TIMEOUT_SECONDS = 300  # 4K drawings take longer than the normal 3 minutes allow


def _without_size(payload: dict[str, Any]) -> dict[str, Any]:
    config = {k: v for k, v in payload["generationConfig"]["imageConfig"].items() if k != "imageSize"}
    return {**payload, "generationConfig": {**payload["generationConfig"], "imageConfig": config}}


def _as_jpeg(image: tuple[bytes, str, str]) -> tuple[bytes, str, str]:
    """Store full-resolution results as high-quality JPEG: a few MB instead of tens."""
    if image[1] == "image/jpeg":
        return image
    with Image.open(io.BytesIO(image[0])) as picture:
        out = io.BytesIO()
        picture.convert("RGB").save(out, format="JPEG", quality=92, optimize=True)
    return out.getvalue(), "image/jpeg", "jpg"
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


# The image model tends to "restyle" garments: long sleeves come out as half sleeves, a shirt gets tucked in one view
# and not the next. These rules, plus facts read from the product photos (see TryOnService._facts), pin them down.
GARMENT_RULES = (
    "Garment construction comes from the product photo and must not change: keep the exact sleeve length (a full-sleeve or "
    "long-sleeve product keeps its sleeves all the way down to the wrists; a half-sleeve stays above the elbow; sleeveless stays "
    "sleeveless; never shorten, lengthen or roll up sleeves), the exact hem length, neckline and collar, cuffs, the number and "
    "position of buttons, pockets and seams, and the fit. "
)
PRODUCT_FACTS_PROMPT = (
    "These are product photos for a virtual try-on: {pieces}. For each product, write short plain sentences stating the construction "
    "facts an artist must copy exactly: sleeve length (sleeveless, short above the elbow, elbow, three-quarter, or full length to the "
    "wrist) and the cuffs; where the hem falls on the body (waist, hip, thigh, knee, calf, ankle); the neckline or collar; the "
    "closure (buttons, zip, none); whether a top is worn tucked in or left out in the photo; the fit (slim, regular, relaxed, "
    "oversized); and any print or pattern. Describe only the listed product, never other clothes the model wears. Start each "
    "sentence with the product, e.g. 'The shirt has full-length sleeves reaching the wrists, with buttoned cuffs.' At most 6 "
    "sentences per product."
)
SPIN_STYLE_PROMPT = (
    "Image 1 is a fashion photo of a person. Write short plain sentences describing exactly how the outfit is built and worn, so the "
    "same outfit can be drawn from the side and the back without any change: whether each top is tucked in, half-tucked or left out "
    "(and over which garment); the sleeve length (to the wrist, elbow, above the elbow, none) and whether sleeves or cuffs are rolled; "
    "whether buttons or zips are done up or open; where each hem falls on the body; what is layered over what; the trouser, skirt or "
    "dress length and leg shape; and the shoes. Describe only what is visible. At most 10 sentences."
)
FACTS_SCHEMA = {"type": "object", "properties": {"facts": {"type": "array", "items": {"type": "string"}}}, "required": ["facts"]}
MAX_FACTS = 20


def _facts_text(facts: list[str] | None, lead: str) -> str:
    return (lead + " ".join(fact.rstrip(".") + "." for fact in facts) + " ") if facts else ""


def tryon_prompt(pieces: list["OutfitPiece"], pose: str = "standard", face_reference: bool = False, person_share: float | None = None,
                 facts: list[str] | None = None) -> str:
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
        + GARMENT_RULES
        + _facts_text(facts, "Facts about the products that must be true in the result: ")
        + "Do not add clothing, jewelry or accessories that were not provided; the person's own glasses and face accessories stay as they are. "
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
        + "Framing: vertical portrait with the entire body in frame from the top of the head to the soles of the shoes, "
        + (f"the person the same size in the picture as in image 1: from head to feet they fill about {round(person_share * 100)}% of the picture height, "
           "with the backdrop visible around them; do not zoom in or make the person bigger than in image 1, "
           if person_share else "a small margin above the head and below the feet, ")
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


# Social poses: the pose is already drawn with natural proportions, so the face pass must not resize the head.
POSE_FACE_REFINE_PROMPT = FACE_REFINE_PROMPT.replace(
    "The head must be the same size relative to the shoulders and body "
    "as in image 3: a real person's head is about one seventh of their height, so never shrink it to fashion-model proportions. ",
    "Keep the head exactly the same size, position and angle as the head already in image 1: do not make the head or face bigger "
    "or smaller; only the face, hair and accessories change to match the real person. ",
)
HEAD_GROWTH_LIMIT = 1.06  # a refined head more than 6% bigger than before is put back at its original size


def _match_body_tone(drawn: tuple[bytes, str, str], edited: tuple[bytes, str, str], label: str) -> tuple[bytes, str, str]:
    """After a face step, bring the face back to the skin tone it had when the whole person was drawn together,
    so face, neck, arms and hands match (see identity.match_face_tone)."""
    try:
        matched = identity.match_face_tone(drawn[0], edited[0], edited[1])
        return (matched, edited[1], edited[2]) if matched else edited
    except Exception:  # a tone polish must never lose a finished look
        log.warning("Could not match the face tone for %s", label, exc_info=True)
        return edited


def _keep_head_size(before: tuple[bytes, str, str], after: tuple[bytes, str, str], label: str) -> tuple[bytes, str, str]:
    """If the face pass enlarged the head, place the refined head back onto the pose at the head size the pose
    had (aligned on the eyes, nose and mouth), so the corrected face keeps natural body proportions."""
    try:
        was, now = identity.face_height(before[0]), identity.face_height(after[0])
        if not was or not now or now <= was * HEAD_GROWTH_LIMIT:
            return after
        placed = identity.lock_face(after[0], before[0], before[1])
        if placed is None:
            log.info("Head grew %.0f%% in the face pass for %s but could not be resized", (now / was - 1) * 100, label)
            return after
        log.info("Head grew %.0f%% in the face pass for %s; put back at its original size", (now / was - 1) * 100, label)
        return placed, before[1], before[2]
    except Exception:  # sizing is a polish step; never lose a finished pose over it
        log.warning("Could not check the head size for %s", label, exc_info=True)
        return after


# 360° view: the finished look is redrawn from these turns around the person (degrees, clockwise seen
# from above; 0 is the saved front-facing image). The viewer spins through front, right, back, left.
SPIN_ANGLES = (90, 180, 270)
SPIN_VIEWS = {
    90: "turned 90 degrees to their left, standing side-on so their nose, chest and toes point to the RIGHT edge of the picture. "
        "The camera sees their right side in full profile: the right cheek, right ear, right arm and right shoulder",
    180: "turned around 180 degrees, so the camera sees them directly from behind: the back of the head and hair, "
         "the back of every garment and the heels of the shoes. The face is not visible",
    270: "turned 90 degrees to their right, standing side-on so their nose, chest and toes point to the LEFT edge of the picture. "
         "The camera sees their left side in full profile: the left cheek, left ear, left arm and left shoulder",
}
# Which way the nose should point in the picture for each side view; a view drawn the wrong way round is mirrored.
SPIN_FACING = {90: "right", 270: "left"}


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


def spin_prompt(angle: int, facts: list[str] | None = None) -> str:
    return (
        "Image 1 is a finished fashion photo of a person. Redraw the same photo with the person " + SPIN_VIEWS[angle] + ". "
        "The person turns on the spot; the camera does not move. Background: the same plain, seamless studio backdrop as image 1, "
        "the same flat colour and brightness from edge to edge, with nothing in it: no room, walls, furniture, props, scenery or floor "
        "pattern, only a faint soft shadow at the feet. "
        "Keep the same person, body size and proportions, height, hairstyle, skin tone, glasses and accessories, and exactly the same "
        "outfit, colours, prints, fabric, fit, length and shoes. "
        "The outfit is styled exactly as in image 1 from every side: a top left out at the front is left out at the back and sides too, "
        "a tucked-in top is tucked in all the way round, and sleeves keep the same length on both arms (full-length sleeves reach the "
        "wrists; never shorten or roll them). Buttons, collars, cuffs, hems and layers stay as they are in image 1. "
        + _facts_text(facts, "How the outfit is built and worn in image 1, which must stay true in this view: ")
        + "Image 2 is the person's own photo and image 3 is a product reference; use them only for details that image 1 does not show, "
        "such as the back of a garment, never for how the garment is worn, and never change what image 1 already shows. "
        "Keep the same camera height and distance and the same framing: the whole body from the top of the head to the soles of the "
        "shoes, the same size and position in the frame as in image 1, standing upright with arms relaxed at the sides. "
        "Photorealistic, one person, no text, no watermark, no collage."
    )


MIN_PERSON_SHARE = 0.35  # a smaller person in the photo is too far away to copy the size from
MAX_PERSON_SHARE = 0.9  # leave a little room above the head and below the feet
PERSON_SIZE_TOLERANCE = 1.1  # drawn up to 10% bigger than the photo is fine


def _keep_person_size(result: tuple[bytes, str, str], target: float) -> tuple[bytes, str, str]:
    """The image model tends to zoom in on the person. When the drawn person is clearly bigger in the picture
    than in their own photo, scale the drawing down to match (plain backdrops only, see identity.shrink_to_share)."""
    try:
        drawn = identity.person_height_share(result[0])
        if not drawn or drawn <= target * PERSON_SIZE_TOLERANCE:
            return result
        shrunk = identity.shrink_to_share(result[0], drawn, target)
        if shrunk is None:
            log.info("Standard pose person is %.0f%% tall vs %.0f%% in the photo; backdrop too busy to resize", drawn * 100, target * 100)
            return result
        log.info("Standard pose person resized from %.0f%% to %.0f%% of the picture height", drawn * 100, target * 100)
        return shrunk, "image/png", "png"
    except Exception:  # sizing is a polish step; never lose a finished look over it
        log.warning("Could not check the person's size in the look", exc_info=True)
        return result


FRONT_BACKDROP_PROMPT = (
    "Image 1 is a fashion photo of a person. Return the same photo with only the background replaced by a plain, seamless, light "
    "warm-grey studio backdrop, flat and even from edge to edge, with a faint soft shadow at the feet. Change nothing about the "
    "person: the same face, hair, body, pose, size and position in the frame, and the same outfit, colours, fit, sleeve length, how "
    "each garment is tucked or buttoned, and the shoes. Photorealistic, one person, no text, no watermark, no borders."
)


def _mirror(view: tuple[bytes, str, str]) -> tuple[bytes, str, str]:
    """The view flipped left to right, kept in its own format (a 4K view as PNG would be about 20 MB)."""
    jpeg = view[1] == "image/jpeg"
    with Image.open(io.BytesIO(view[0])) as image:
        out = io.BytesIO()
        ImageOps.mirror(image.convert("RGB")).save(out, format="JPEG" if jpeg else "PNG", **({"quality": 92} if jpeg else {}))
    return (out.getvalue(), "image/jpeg", "jpg") if jpeg else (out.getvalue(), "image/png", "png")


def _face_the_right_way(view: tuple[bytes, str, str], angle: int) -> tuple[bytes, str, str]:
    """The image model often draws both side views facing the same way. Mirror a side view whose face points
    the wrong way, so the right and left sides really are opposite. Front and back views are left alone."""
    expected = SPIN_FACING.get(angle)
    if expected is None:
        return view
    try:
        drawn = identity.facing(view[0])
        if drawn is None or drawn == expected:
            return view
        log.info("360 view %s faced %s instead of %s; mirrored it", angle, drawn, expected)
        return _mirror(view)
    except Exception:  # a failed check must never lose a finished view
        log.warning("Could not check which way 360 view %s faces", angle, exc_info=True)
        return view


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
        self.backup = BackupTables(self)
        # One full-resolution image at a time, server-wide: a 4K answer arrives as 30 to 40 MB of base64 text and
        # is several copies of that in memory while it is decoded, so two at once could exhaust a small server.
        self._full_res = asyncio.Semaphore(1)
        self._full_size_refused = False

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

    def ensure_configured(self, needs_ai: bool = True) -> None:
        """Fail fast when Render is missing a setting. Storage-only features (wardrobe, gallery) pass needs_ai=False,
        so they keep working without the image model. Shoppers get a plain message; the log names the setting."""
        missing = []
        if needs_ai and not self.settings.gemini_api_key.get_secret_value(): missing.append("GEMINI_API_KEY")
        if not self.settings.supabase_url: missing.append("SUPABASE_URL")
        if not self.settings.supabase_service_role_key.get_secret_value(): missing.append("SUPABASE_SERVICE_ROLE_KEY")
        if not self.settings.anonymous_token_secret.get_secret_value(): missing.append("ANONYMOUS_TOKEN_SECRET")
        if missing:
            log.error("Not configured, set in Render: %s", ", ".join(missing))
            raise TryOnError("MyDripCheck is being set up right now. Please try again in a few minutes.", 503)

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
        async def nothing() -> None:
            return None

        # The face reference, how big the person is in their own photo (the standard pose keeps them that size instead
        # of zooming in) and the products' construction facts (sleeve length and the like) are read side by side.
        face, share, facts = await asyncio.gather(
            asyncio.to_thread(identity.face_reference, person[0]) if self.settings.face_lock_enabled else nothing(),
            asyncio.to_thread(identity.person_height_share, person[0]) if pose == "standard" else nothing(),
            self._facts([{"text": PRODUCT_FACTS_PROMPT.format(pieces=_describe_pieces(pieces, 1))}, *(self._inline_part(piece.image) for piece in pieces)],
                        "product"),
        )
        target = min(share, MAX_PERSON_SHARE) if share and MIN_PERSON_SHARE <= share <= 1.0 else None
        prompt = tryon_prompt(pieces, pose, face_reference=face is not None, person_share=target, facts=facts)
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
        if target:
            result = await asyncio.to_thread(_keep_person_size, result, target)
        elif pose == "keep" and self.settings.face_lock_enabled:
            # In the person's own pose the head barely moves, so pasting their real features is safe.
            locked = await asyncio.to_thread(identity.lock_face, person[0], result[0], result[1])
            if locked:
                result = await asyncio.to_thread(_match_body_tone, result, (locked, result[1], result[2]), "my pose")
        return result

    async def _refine_face(self, image: tuple[bytes, str, str], person: tuple[bytes, str, str], face: tuple[bytes, str], aspect: str, label: str, check: bool = True, keep_head_size: bool = False) -> tuple[bytes, str, str]:
        refined = await self._refine_face_pass(image, person, face, aspect, label, check, POSE_FACE_REFINE_PROMPT if keep_head_size else FACE_REFINE_PROMPT)
        if keep_head_size and refined is not image:
            refined = await asyncio.to_thread(_keep_head_size, image, refined, label)
        if refined is not image:
            refined = await asyncio.to_thread(_match_body_tone, image, refined, label)
        return refined

    async def _refine_face_pass(self, image: tuple[bytes, str, str], person: tuple[bytes, str, str], face: tuple[bytes, str], aspect: str, label: str, check: bool, prompt: str) -> tuple[bytes, str, str]:
        """Edit-only pass that redraws the head from the real references. With check (Plus and Pro), the
        result is scored with face recognition against the real photo; below the target it is redrawn again
        and the closest version wins (the unrefined image included). Without check, or without a readable
        face, the refined image is used as it is."""
        refine = {"contents": [{"role": "user", "parts": [
            {"text": prompt}, self._inline_part(image), self._inline_part((face[0], face[1], "jpg")), self._inline_part(person),
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

    @property
    def full_size(self) -> str | None:
        """The imageSize for paid output ("4K"), or None when the final full-resolution pass is switched off."""
        if self._full_size_refused:
            return None
        size = self.settings.paid_image_size.strip().upper()
        return size if size in ("2K", "4K") else None

    async def upscale(self, image: tuple[bytes, str, str], label: str = "look") -> tuple[bytes, str, str]:
        """The paid plans' final pass: the same finished photo returned at full resolution (4K) and stored as JPEG.
        Never loses a look: if the pass fails, the normal-resolution image is kept."""
        size = self.full_size
        if not size:
            return image
        payload = {"contents": [{"role": "user", "parts": [{"text": UPSCALE_PROMPT}, self._inline_part(image)]}],
                   "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": _aspect_of(image), "imageSize": size}}}
        try:
            sharp = await self._generated_image(payload, f"Gemini {size} pass failed")
            result = await asyncio.to_thread(_as_jpeg, sharp)
        except Exception as exc:  # a failed polish step keeps the finished look
            log.warning("%s pass for the %s failed, keeping the normal-resolution image: %r", size, label, exc)
            return image
        log.info("%s %s ready: %d KB", size, label, len(result[0]) // 1024)
        return result

    async def _facts(self, parts: list[dict[str, Any]], label: str) -> list[str]:
        """Plain facts about garments (sleeve length, tucked or not...) read by the text model, to pin them in an image prompt.
        Optional: on any failure the image is drawn without them."""
        if not self.settings.garment_facts_enabled:
            return []
        try:
            result = await asyncio.wait_for(self.generate_json(parts, FACTS_SCHEMA, temperature=0), timeout=30)
        except Exception as exc:
            log.info("No %s facts for the image prompt: %r", label, exc)
            return []
        facts = [fact.strip() for fact in (result.get("facts") if isinstance(result, dict) else None) or [] if isinstance(fact, str) and fact.strip()]
        log.info("%s facts: %s", label.capitalize(), " | ".join(facts[:MAX_FACTS]))
        return facts[:MAX_FACTS]

    async def generate_spin_view(self, look: tuple[bytes, str, str], person: tuple[bytes, str, str], product: tuple[bytes, str, str], angle: int,
                                 facts: list[str] | None = None) -> tuple[bytes, str, str]:
        """Draw one side or back view of a finished look, for the 360° viewer. Retries once, since one bad view breaks the spin."""
        payload = {
            "contents": [{"role": "user", "parts": [
                {"text": spin_prompt(angle, facts)}, self._inline_part(look), self._inline_part(person), self._inline_part(product),
            ]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": "3:4", **({"imageSize": self.full_size} if self.full_size else {})}},
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
            image = await self._refine_face(image, person, face, "4:5", f"pose {pose.key}", check=True, keep_head_size=True)
        image = await self.upscale(image, f"pose {pose.key}")  # poses are a Plus and Pro feature: always full resolution
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

    async def _plain_front(self, look: tuple[bytes, str, str]) -> tuple[bytes, str, str] | None:
        """The 360° view turns on a plain studio backdrop. A look on a real background gets a front view with the
        backdrop swapped (None when it is already plain, or the swap failed: then the look itself is the front)."""
        if await asyncio.to_thread(identity.is_plain_backdrop, look[0]):
            return None
        payload = {"contents": [{"role": "user", "parts": [{"text": FRONT_BACKDROP_PROMPT}, self._inline_part(look)]}],
                   "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": "3:4", **({"imageSize": self.full_size} if self.full_size else {})}}}
        try:
            return await self._generated_image(payload, "Gemini 360 backdrop failed")
        except TryOnError as exc:
            if exc.status_code == 429:
                raise
            log.warning("Could not put the 360 front on a plain backdrop: %s", exc)
            return None

    async def create_spin(self, user_id: str, row: dict[str, Any]) -> GalleryItem:
        """Draw the right, back and left views of a saved look and store them next to it.

        Strict on consistency: all views stand on one plain backdrop, the way the outfit is worn (tucked, sleeves,
        buttons) is read from the front once and locked into every view, and the left side is the right side
        mirrored, so the two sides always face opposite ways."""
        look, person, product = await asyncio.gather(
            self.download(row["result_path"]), self.download(row["person_path"]), self.download(row["product_path"]))
        new_front, facts = await asyncio.gather(
            self._plain_front(look), self._facts([{"text": SPIN_STYLE_PROMPT}, self._inline_part(look)], "360 styling"))
        front = new_front or look
        right, back = await asyncio.gather(*(self.generate_spin_view(front, person, product, angle, facts) for angle in (90, 180)))
        right = await asyncio.to_thread(_face_the_right_way, right, 90)
        left = await asyncio.to_thread(_mirror, right)
        views = [right, back, left]  # the order of SPIN_ANGLES: 90, 180, 270
        if self.full_size:  # full-resolution views are stored as JPEG
            views = list(await asyncio.gather(*(asyncio.to_thread(_as_jpeg, view) for view in views)))
            new_front = await asyncio.to_thread(_as_jpeg, new_front) if new_front else None
        prefix = f"{user_id}/{row['id']}"
        paths = [f"{prefix}/spin_{angle}_{uuid4().hex[:8]}.{view[2]}" for angle, view in zip(SPIN_ANGLES, views)]
        front_path = f"{prefix}/spin_0_{uuid4().hex[:8]}.{new_front[2]}" if new_front else None
        uploads = [*zip(paths, views), *([(front_path, new_front)] if new_front else [])]
        await asyncio.gather(*(self._upload(path, view) for path, view in uploads))
        if front_path:
            paths.append(front_path)  # cleaned up with the others if saving fails
        spin = [front_path or row["result_path"], *paths[:3]]
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
        config = (payload.get("generationConfig") or {}).get("imageConfig") or {}
        if "imageSize" not in config:
            return await self._decoded_image(payload, failure)
        if self._full_size_refused:
            return await self._decoded_image(_without_size(payload), failure)
        async with self._full_res:
            try:
                image = await self._decoded_image(payload, failure)
            except TryOnError as exc:
                if exc.status_code == 400 or "imageSize" in str(exc) or "image_size" in str(exc).lower():
                    # The model refused the size: draw at the normal size from now on rather than fail every request.
                    self._full_size_refused = True
                    log.error("Gemini refused imageSize=%s, using the normal size from now on: %s", config["imageSize"], exc)
                    return await self._decoded_image(_without_size(payload), failure)
                raise
            return await asyncio.to_thread(_as_jpeg, image)  # a few MB instead of tens, before the next one starts

    async def _decoded_image(self, payload: dict[str, Any], failure: str) -> tuple[bytes, str, str]:
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
        mime = image.get("mime_type") or image.get("mimeType") or "image/png"
        del body, image  # drop the base64 text before decoding the picture
        return validate_image(data, mime, GENERATED_IMAGE_MAX_BYTES)

    async def generate_json(self, parts: list[dict[str, Any]], schema: dict[str, Any], temperature: float = 0.7) -> Any:
        """Ask the Gemini text model for JSON that matches ``schema``."""
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": temperature},
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
        big = "imageSize" in ((payload.get("generationConfig") or {}).get("imageConfig") or {})
        async with httpx.AsyncClient(timeout=FULL_RES_TIMEOUT_SECONDS if big else 180) as client:
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
        url = f"{self.settings.supabase_url.rstrip('/')}/rest/v1/{table}"
        if table in BACKED_UP_TABLES:
            return await self.backup.rest(method, table, params=params, json_body=json_body, prefer=prefer,
                                          real=lambda: self._sb(method, url, headers=headers, params=params, json=json_body))
        return await self._sb(method, url, headers=headers, params=params, json=json_body)

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
