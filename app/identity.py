"""Face lock: put the person's real face back onto a generated try-on.

Image models redraw the face on every generation, so a re-posed catalogue photo can look like a
different person. After generation we find the face in the user's photo and in the result
(YuNet, MIT licensed, runs on CPU), align the real face to where the model drew the head, match
its colour to the new lighting, and blend it in with a soft oval mask. Hair, ears, neck and body
stay as generated. When the head is turned differently in the two images, or a face cannot be
found, the result is returned untouched rather than risking a bad paste.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).parent / "assets" / "face_detection_yunet_2023mar.onnx"
MIN_SCORE = 0.8
MIN_EYE_DISTANCE_PX = 18  # smaller faces are too low resolution to improve
MAX_TURN_DIFFERENCE = 0.14  # difference in head turn (nose offset / eye distance) we still accept
MAX_TILT_DIFFERENCE_DEG = 25
# How far to move the real face towards the generated lighting (0..1): brightness follows the new
# studio light closely, colour only a little so the person's real skin tone is kept.
MATCH_BRIGHTNESS = 0.7
MATCH_COLOUR = 0.25


@dataclass
class Face:
    box: np.ndarray  # x, y, w, h
    points: np.ndarray  # 5x2: right eye, left eye, nose tip, right mouth corner, left mouth corner
    score: float

    @property
    def eye_distance(self) -> float:
        return float(np.linalg.norm(self.points[1] - self.points[0]))

    @property
    def turn(self) -> float:
        """How far the nose sits from the middle of the eyes, relative to eye distance (0 = facing the camera)."""
        middle = (self.points[0] + self.points[1]) / 2
        return float((self.points[2][0] - middle[0]) / max(self.eye_distance, 1e-6))

    @property
    def tilt(self) -> float:
        dx, dy = self.points[1] - self.points[0]
        return float(np.degrees(np.arctan2(dy, dx)))


_detector = None


def _detect(image: np.ndarray) -> Face | None:
    global _detector
    if _detector is None:
        _detector = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (320, 320), MIN_SCORE)
    height, width = image.shape[:2]
    scale = min(1.0, 1280 / max(height, width))  # YuNet is fast; keep big photos at a sane size
    work = cv2.resize(image, (round(width * scale), round(height * scale))) if scale < 1 else image
    _detector.setInputSize((work.shape[1], work.shape[0]))
    _, faces = _detector.detect(work)
    if faces is None or not len(faces):
        return None
    best = max(faces, key=lambda f: f[2] * f[3])
    return Face(box=best[:4] / scale, points=best[4:14].reshape(5, 2) / scale, score=float(best[14]))


def _face_mask(face: Face, shape: tuple[int, int]) -> np.ndarray:
    """Soft oval over eyebrows, eyes, nose, mouth and chin; hairline and ears stay generated."""
    eye_mid = (face.points[0] + face.points[1]) / 2
    mouth_mid = (face.points[3] + face.points[4]) / 2
    down = mouth_mid - eye_mid
    down_len = max(float(np.linalg.norm(down)), 1e-6)
    center = eye_mid + down * 0.42
    angle = float(np.degrees(np.arctan2(down[0], down[1])))  # rotation of the eye->mouth axis from vertical
    half_width = face.eye_distance * 0.98
    half_height = down_len * 1.55
    mask = np.zeros(shape, np.float32)
    cv2.ellipse(mask, (round(center[0]), round(center[1])), (round(half_width), round(half_height)), -angle, 0, 360, 1.0, -1)
    feather = max(3, int(face.eye_distance * 0.35)) | 1
    return cv2.GaussianBlur(mask, (feather, feather), 0)


def _match_colour(source: np.ndarray, target: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Shift the real face's colour statistics towards the generated face so the light matches."""
    src = cv2.cvtColor(source, cv2.COLOR_BGR2LAB).astype(np.float32)
    dst = cv2.cvtColor(target, cv2.COLOR_BGR2LAB).astype(np.float32)
    inside = mask > 0.5
    if inside.sum() < 50:
        return source
    out = src.copy()
    for channel, amount in ((0, MATCH_BRIGHTNESS), (1, MATCH_COLOUR), (2, MATCH_COLOUR)):
        s_mean, s_std = src[..., channel][inside].mean(), src[..., channel][inside].std() + 1e-6
        d_mean, d_std = dst[..., channel][inside].mean(), dst[..., channel][inside].std() + 1e-6
        matched = (src[..., channel] - s_mean) * (d_std / s_std) + d_mean
        out[..., channel] = src[..., channel] * (1 - amount) + matched * amount
    return cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)


def lock_face(original: bytes, generated: bytes, output_mime: str = "image/png") -> bytes | None:
    """Return the generated image with the real face blended in, or None when it is not safe to do."""
    try:
        person = cv2.imdecode(np.frombuffer(original, np.uint8), cv2.IMREAD_COLOR)
        result = cv2.imdecode(np.frombuffer(generated, np.uint8), cv2.IMREAD_COLOR)
        if person is None or result is None:
            return None
        real, drawn = _detect(person), _detect(result)
        if real is None or drawn is None:
            log.info("Face lock skipped: face not found (photo=%s, result=%s)", real is not None, drawn is not None)
            return None
        if min(real.eye_distance, drawn.eye_distance) < MIN_EYE_DISTANCE_PX:
            log.info("Face lock skipped: face too small")
            return None
        if abs(real.turn - drawn.turn) > MAX_TURN_DIFFERENCE or abs(real.tilt - drawn.tilt) > MAX_TILT_DIFFERENCE_DEG:
            log.info("Face lock skipped: head angle differs (turn %.2f vs %.2f)", real.turn, drawn.turn)
            return None
        # Similarity transform (move, rotate, uniform scale) from the real face onto the generated head.
        matrix, inliers = cv2.estimateAffinePartial2D(real.points.astype(np.float32), drawn.points.astype(np.float32), method=cv2.LMEDS)
        if matrix is None:
            return None
        scale = float(np.hypot(matrix[0, 0], matrix[1, 0]))
        if not 0.2 < scale < 5:
            return None
        height, width = result.shape[:2]
        warped = cv2.warpAffine(person, matrix, (width, height), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REFLECT)
        mask = _face_mask(drawn, (height, width))
        # Only paste where the real face actually exists (not reflected border pixels).
        coverage = cv2.warpAffine(np.ones(person.shape[:2], np.float32), matrix, (width, height), flags=cv2.INTER_NEAREST)
        mask *= coverage
        warped = _match_colour(warped, result, mask)
        alpha = mask[..., None]
        blended = (warped.astype(np.float32) * alpha + result.astype(np.float32) * (1 - alpha)).astype(np.uint8)
        extension = ".jpg" if output_mime == "image/jpeg" else ".webp" if output_mime == "image/webp" else ".png"
        params = [cv2.IMWRITE_JPEG_QUALITY, 95] if extension == ".jpg" else []
        ok, encoded = cv2.imencode(extension, blended, params)
        return encoded.tobytes() if ok else None
    except cv2.error:
        log.exception("Face lock failed")
        return None


def face_reference(original: bytes, max_side: int = 768) -> tuple[bytes, str] | None:
    """A close-up of the person's head from the full-resolution photo, sent to the model as an identity reference."""
    try:
        person = cv2.imdecode(np.frombuffer(original, np.uint8), cv2.IMREAD_COLOR)
        if person is None:
            return None
        face = _detect(person)
        if face is None or face.eye_distance < MIN_EYE_DISTANCE_PX:
            return None
        x, y, w, h = face.box
        cx, cy, side = x + w / 2, y + h * 0.45, max(w, h) * 1.9  # head with hair and chin, a little neck
        height, width = person.shape[:2]
        left, top = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        right, bottom = int(min(width, cx + side / 2)), int(min(height, cy + side / 2))
        crop = person[top:bottom, left:right]
        if crop.size == 0:
            return None
        scale = min(1.0, max_side / max(crop.shape[:2]))
        if scale < 1:
            crop = cv2.resize(crop, (round(crop.shape[1] * scale), round(crop.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return (encoded.tobytes(), "image/jpeg") if ok else None
    except cv2.error:
        log.exception("Face reference failed")
        return None
