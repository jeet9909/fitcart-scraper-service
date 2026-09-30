"""Face lock: put the person's real head back onto a try-on made in their own pose ("keep" pose).

Image models redraw the face on every generation, so even in the person's own pose the face drifts. In
that pose the head does not move, so we find the face in the user's photo and in the result (YuNet, MIT
licensed, runs on CPU), align the real photo to where the model drew the head, and fade the whole real
head in: hair, forehead, glasses, eyes, nose, mouth, beard and face shape, down to just under the chin.
Blending the whole head (not only the eyes, nose and mouth) means no seam runs through the glasses or
the beard, which is what made earlier results look doubled or smeared. The collar and clothes below the
chin stay as generated. When the head angle or face shape differs too much, or a face cannot be found,
the result is returned untouched rather than risking a bad paste.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).parent / "assets" / "face_detection_yunet_2023mar.onnx"
# SFace (Apache 2.0, OpenCV Zoo): turns an aligned face into a 128-number signature; the cosine of two
# signatures says how alike two faces are (same person is roughly above 0.36; our results sit 0.5-0.9).
RECOGNIZER_PATH = Path(__file__).parent / "assets" / "face_recognition_sface_2021dec_int8.onnx"
MIN_SCORE = 0.8
MIN_EYE_DISTANCE_PX = 18  # smaller faces are too low resolution to improve
MAX_TURN_DIFFERENCE = 0.14  # difference in head turn (nose offset / eye distance) we still accept
MAX_TILT_DIFFERENCE_DEG = 25
MAX_ALIGNMENT_ERROR = 0.06  # mean landmark distance after alignment / eye distance; above this the face shapes differ


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
_recognizer = None


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


def _similarity(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Least-squares move + rotate + uniform scale mapping source points onto target points (Umeyama).
    Unlike a robust fit it uses all five landmarks, so a differently shaped face shows up as error."""
    src_mean, dst_mean = source.mean(axis=0), target.mean(axis=0)
    src, dst = source - src_mean, target - dst_mean
    u, singular, vt = np.linalg.svd(dst.T @ src / len(source))
    sign = np.diag([1.0, np.sign(np.linalg.det(u @ vt))])
    rotation = u @ sign @ vt
    scale = float(np.trace(np.diag(singular) @ sign) / max((src ** 2).sum() / len(source), 1e-9))
    matrix = np.zeros((2, 3), np.float64)
    matrix[:, :2] = scale * rotation
    matrix[:, 2] = dst_mean - matrix[:, :2] @ src_mean
    return matrix


def _alignment_error(real: Face, drawn: Face, matrix: np.ndarray) -> float:
    """How far the real landmarks land from the drawn ones after alignment, relative to eye distance."""
    moved = real.points @ matrix[:, :2].T + matrix[:, 2]
    return float(np.linalg.norm(moved - drawn.points, axis=1).mean() / max(drawn.eye_distance, 1e-6))


def _head_mask(face: Face, shape: tuple[int, int]) -> np.ndarray:
    """Soft mask (0-1) of the whole head: hair, ears, glasses and beard, ending just under the chin."""
    x, y, w, h = face.box
    height, width = shape
    mask = np.zeros(shape, np.float32)
    center = (int(round(x + w / 2)), int(round(y + h * 0.30)))
    axes = (int(round(w * 0.66)), int(round(h * 0.80)))
    cv2.ellipse(mask, center, axes, face.tilt, 0, 360, 1.0, -1)
    chin = int(round(y + h * 1.06))
    mask[min(max(chin, 0), height):, :] = 0  # the collar and clothes below the chin stay generated
    feather = max(3, int(round(w * 0.07))) | 1
    return cv2.GaussianBlur(mask, (feather * 4 + 1, feather * 4 + 1), feather)


def lock_face(original: bytes, generated: bytes, output_mime: str = "image/png") -> bytes | None:
    """Return the generated image with the real head faded in, or None when it is not safe to do."""
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
        matrix = _similarity(real.points.astype(np.float64), drawn.points.astype(np.float64))
        scale = float(np.hypot(matrix[0, 0], matrix[1, 0]))
        if not 0.2 < scale < 5:
            return None
        error = _alignment_error(real, drawn, matrix)
        if error > MAX_ALIGNMENT_ERROR:
            log.info("Face lock skipped: the generated face shape differs too much to blend cleanly")
            return None
        height, width = result.shape[:2]
        warped = cv2.warpAffine(person, matrix, (width, height), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REFLECT)
        # Only use pixels the real photo actually has (not the reflected border).
        coverage = cv2.warpAffine(np.ones(person.shape[:2], np.float32), matrix, (width, height), flags=cv2.INTER_LINEAR)
        alpha = (_head_mask(drawn, (height, width)) * coverage)[..., None]
        if float(alpha.max()) < 0.5:
            return None
        blended = (warped.astype(np.float32) * alpha + result.astype(np.float32) * (1 - alpha)).round().clip(0, 255).astype(np.uint8)
        log.info("Face lock applied (whole head): eye distance %.0f px (photo) -> %.0f px (result), alignment error %.3f",
                 real.eye_distance, drawn.eye_distance, error)
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


def face_signature(image_bytes: bytes) -> np.ndarray | None:
    """The identity signature of the main face in a photo, or None when no clear face is found."""
    global _recognizer
    try:
        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return None
        face = _detect(image)
        if face is None or face.eye_distance < MIN_EYE_DISTANCE_PX:
            return None
        if _recognizer is None:
            _recognizer = cv2.FaceRecognizerSF.create(str(RECOGNIZER_PATH), "")
        row = np.concatenate([face.box, face.points.reshape(-1), [face.score]]).astype(np.float32)
        return _recognizer.feature(_recognizer.alignCrop(image, row)).copy()
    except cv2.error:
        log.exception("Face signature failed")
        return None


def face_match(real: np.ndarray | None, image_bytes: bytes) -> float | None:
    """How much the face in image_bytes looks like the real signature (cosine, higher is closer)."""
    if real is None:
        return None
    other = face_signature(image_bytes)
    if other is None or _recognizer is None:
        return None
    return float(_recognizer.match(real, other, cv2.FaceRecognizerSF_FR_COSINE))
