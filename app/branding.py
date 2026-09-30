"""MyDripCheck watermark for looks made on the free plan: a small badge in the top-right corner, which in a
full-body look is background (the shoes reach the bottom edge)."""

import io
import logging
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

BADGE_PATH = Path(__file__).parent / "assets" / "watermark.png"
BADGE_WIDTH = 0.26  # of the image width
MARGIN = 0.03  # of the image width, from the right and top edges
_badge: Image.Image | None = None


def watermark(image: tuple[bytes, str, str]) -> tuple[bytes, str, str]:
    """Return the image with the badge on it, in the same format. On any problem the image is returned as is."""
    global _badge
    data, mime, ext = image
    try:
        if _badge is None:
            _badge = Image.open(BADGE_PATH).convert("RGBA")
        with Image.open(io.BytesIO(data)) as source:
            base = source.convert("RGBA")
        width = max(140, round(base.width * BADGE_WIDTH))
        badge = _badge.resize((width, round(width * _badge.height / _badge.width)), Image.LANCZOS)
        margin = round(base.width * MARGIN)
        base.alpha_composite(badge, (base.width - badge.width - margin, margin))
        out = io.BytesIO()
        if mime == "image/jpeg":
            base.convert("RGB").save(out, "JPEG", quality=92)
        elif mime == "image/webp":
            base.save(out, "WEBP", quality=92)
        else:
            base.save(out, "PNG", optimize=True)
        return out.getvalue(), mime, ext
    except Exception:  # a missing watermark must never lose the user's look
        log.exception("Watermark failed; saving the look without it")
        return image
