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
import threading
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
# One detector and one recognizer are shared by every request, and OpenCV's networks are not thread-safe:
# two looks checked at the same moment crashed inside forward(). Calls take turns (each is a few milliseconds).
_model_lock = threading.RLock()


def _detect(image: np.ndarray) -> Face | None:
    with _model_lock:
        return _detect_unlocked(image)


def _detect_unlocked(image: np.ndarray) -> Face | None:
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
        row = np.concatenate([face.box, face.points.reshape(-1), [face.score]]).astype(np.float32)
        with _model_lock:
            if _recognizer is None:
                _recognizer = cv2.FaceRecognizerSF.create(str(RECOGNIZER_PATH), "")
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
    with _model_lock:
        return float(_recognizer.match(real, other, cv2.FaceRecognizerSF_FR_COSINE))


def facing(image_bytes: bytes) -> str | None:
    """Which way a face in profile points in the picture: 'left', 'right', or None when it faces the camera,
    is turned away, or no face is found. Measured as the nose tip against the eyes and mouth corners."""
    image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    face = _detect(image)
    if face is None:
        return None
    others = np.concatenate([face.points[:2, 0], face.points[3:, 0]])
    offset = (face.points[2][0] - float(others.mean())) / max(float(face.box[2]), 1.0)
    if abs(offset) < PROFILE_OFFSET:
        return None
    return "right" if offset > 0 else "left"


PROFILE_OFFSET = 0.12  # nose this far (share of face width) from the eyes and mouth means a side view


BODY_PER_FACE_BOX = 6.1  # a standing adult, head to feet, is about six YuNet face boxes tall (measured on real looks)


def person_height_share(image_bytes: bytes) -> float | None:
    """Roughly how much of the picture height a standing person fills, head to feet, from the size of their face."""
    image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    face = _detect(image)
    if face is None:
        return None
    return float(face.box[3]) * BODY_PER_FACE_BOX / image.shape[0]


def shrink_to_share(image_bytes: bytes, share_now: float, share_wanted: float) -> bytes | None:
    """Make the person smaller in a plain-backdrop picture: scale it down around the feet and fill the freed
    edges with the backdrop colour. Returns None when the backdrop is not plain enough to extend cleanly."""
    image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if image is None or share_now <= 0:
        return None
    height, width = image.shape[:2]
    band = max(4, round(min(height, width) * 0.05))
    if _edge_roughness(image, band) > PLAIN_BACKDROP_ROUGHNESS:
        return None
    factor = max(MIN_SHRINK, share_wanted / share_now)
    small = cv2.resize(image, (round(width * factor), round(height * factor)), interpolation=cv2.INTER_AREA)
    # Extend the backdrop from the picture's own left and right edges (never the person), blending across,
    # which keeps the backdrop's light falloff and tint.
    left = image[:, :band].astype(np.float32).mean(axis=1)
    right = image[:, -band:].astype(np.float32).mean(axis=1)
    across = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :, None]
    backdrop = left[:, None, :] * (1 - across) + right[:, None, :] * across
    backdrop = cv2.GaussianBlur(backdrop, (0, 0), band)
    x, y = (width - small.shape[1]) // 2, height - small.shape[0]
    pasted = backdrop.copy()
    pasted[y:, x:x + small.shape[1]] = small
    # Fade the smaller picture's edges into the extended backdrop so no seam shows.
    mask = np.zeros((height, width), np.float32)
    inset = max(2, round(width * 0.03))
    mask[y + inset:, x + inset:x + small.shape[1] - inset] = 1
    mask = cv2.GaussianBlur(mask, (0, 0), inset)[..., None]
    out = backdrop * (1 - mask) + pasted * mask
    ok, encoded = cv2.imencode(".png", np.clip(out, 0, 255).astype(np.uint8))
    return encoded.tobytes() if ok else None


ALIGN_WORK_SIDE = 1024  # features are matched on a copy this size; the warp itself runs at full size
ALIGN_MIN_INLIERS = 25  # background points that must agree before a picture is moved
ALIGN_MAX_ROTATION_DEG = 4
ALIGN_SCALE_RANGE = (0.6, 1.5)
ALIGN_SKIP_SCALE = 0.015  # closer than this (and a tiny shift) already lines up; leave it untouched
ALIGN_SKIP_SHIFT = 0.006


def align_to_reference(reference: bytes, generated: bytes, output_mime: str = "image/png") -> bytes | None:
    """Put a generated picture back into the reference photo's framing: same aspect ratio, and the scene (the
    person's own background) at the same size and position, so a before/after slider lines up and the person is
    not zoomed in. The match comes from background details both pictures share (ORB features, RANSAC), so it
    needs the same place in both: in the person's own pose the background is kept. Any strip the moved picture no
    longer covers is filled from the reference. Returns None when the pictures do not match well enough, or
    already line up."""
    try:
        ref = cv2.imdecode(np.frombuffer(reference, np.uint8), cv2.IMREAD_COLOR)
        gen = cv2.imdecode(np.frombuffer(generated, np.uint8), cv2.IMREAD_COLOR)
        if ref is None or gen is None:
            return None
        ref_scale = min(1.0, ALIGN_WORK_SIDE / max(ref.shape[:2]))
        gen_scale = min(1.0, ALIGN_WORK_SIDE / max(gen.shape[:2]))
        small_ref = cv2.cvtColor(cv2.resize(ref, None, fx=ref_scale, fy=ref_scale, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        small_gen = cv2.cvtColor(cv2.resize(gen, None, fx=gen_scale, fy=gen_scale, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        orb = cv2.ORB_create(nfeatures=4000)
        ref_points, ref_desc = orb.detectAndCompute(small_ref, None)
        gen_points, gen_desc = orb.detectAndCompute(small_gen, None)
        if ref_desc is None or gen_desc is None or len(ref_points) < ALIGN_MIN_INLIERS or len(gen_points) < ALIGN_MIN_INLIERS:
            return None
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(gen_desc, ref_desc, k=2)
        good = [m for m, *rest in pairs if rest and m.distance < 0.75 * rest[0].distance]
        if len(good) < ALIGN_MIN_INLIERS:
            log.info("Framing match skipped: only %d matching background points", len(good))
            return None
        src = np.float32([gen_points[m.queryIdx].pt for m in good]) / gen_scale
        dst = np.float32([ref_points[m.trainIdx].pt for m in good]) / ref_scale
        matrix, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0 / ref_scale)
        count = int(inliers.sum()) if inliers is not None else 0
        if matrix is None or count < ALIGN_MIN_INLIERS:
            log.info("Framing match skipped: %d background points agree", count)
            return None
        scale = float(np.hypot(matrix[0, 0], matrix[1, 0]))
        rotation = float(np.degrees(np.arctan2(matrix[1, 0], matrix[0, 0])))
        if abs(rotation) > ALIGN_MAX_ROTATION_DEG:
            return None
        # The output keeps the generated picture's resolution but the reference photo's shape.
        out_scale = max(gen.shape[:2]) / max(ref.shape[:2])
        height, width = round(ref.shape[0] * out_scale), round(ref.shape[1] * out_scale)
        matrix = matrix * out_scale  # generated pixels -> output pixels
        zoom = max(ref.shape[:2]) / (scale * max(gen.shape[:2]))  # how much bigger the scene was drawn than in the photo
        if not ALIGN_SCALE_RANGE[0] < zoom < ALIGN_SCALE_RANGE[1]:
            log.info("Framing match skipped: implausible zoom %.2f", zoom)
            return None
        same_shape = abs(gen.shape[0] / gen.shape[1] - ref.shape[0] / ref.shape[1]) < 0.01
        shift = float(np.hypot(matrix[0, 2], matrix[1, 2])) / max(width, height)
        if same_shape and abs(zoom - 1) < ALIGN_SKIP_SCALE and abs(rotation) < 0.5 and shift < ALIGN_SKIP_SHIFT:
            return None
        warped = cv2.warpAffine(gen, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT)
        covered = cv2.warpAffine(np.full(gen.shape[:2], 255, np.uint8), matrix, (width, height), flags=cv2.INTER_NEAREST)
        covered = cv2.erode(covered, np.ones((3, 3), np.uint8), iterations=2)
        gap = covered < 255
        if gap.any():
            filler = cv2.resize(ref, (width, height), interpolation=cv2.INTER_CUBIC if out_scale > 1 else cv2.INTER_AREA)
            warped[gap] = filler[gap]
            del filler
        log.info("Framing matched to the photo: person drawn at %.0f%% of the photo's size, %d background points", zoom * 100, count)
        extension = ".jpg" if output_mime == "image/jpeg" else ".png"
        params = [cv2.IMWRITE_JPEG_QUALITY, 92] if extension == ".jpg" else []
        ok, encoded = cv2.imencode(extension, warped, params)
        return encoded.tobytes() if ok else None
    except cv2.error:
        log.exception("Framing match failed")
        return None


def is_plain_backdrop(image_bytes: bytes) -> bool:
    """True when the picture's left and right edges are a smooth studio backdrop rather than a real place."""
    image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return False
    band = max(4, round(min(image.shape[:2]) * 0.05))
    return _edge_roughness(image, band) <= PLAIN_BACKDROP_ROUGHNESS


PLAIN_BACKDROP_ROUGHNESS = 1.5  # studio backdrops measure about 0.6, real places (plants, walls, bikes) 3 and up


def _edge_roughness(image: np.ndarray, band: int) -> float:
    """Fine detail along the left and right edges (mean Laplacian, floor excluded). Smooth gradients, the
    usual studio backdrop, score low; a real background scores high."""
    grey = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32), (0, 0), 1.0)
    detail = np.abs(cv2.Laplacian(grey, cv2.CV_32F))
    rows = slice(0, int(image.shape[0] * 0.85))
    return float(np.concatenate([detail[rows, :band], detail[rows, -band:]], axis=1).mean())
MIN_SHRINK = 0.7  # never shrink by more than 30%: past that the drawing itself is off and a redraw is better


def face_height(image_bytes: bytes) -> float | None:
    """Height in pixels of the main face, for comparing head size between two versions of the same picture."""
    image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    face = _detect(image)
    return float(face.box[3]) if face is not None else None


TONE_SHIFT_LIMIT = 3.0  # colour difference (Lab) between the two faces below which nothing is changed
TONE_MAX_SHIFT = 25.0  # never move the face colour further than this, whatever the measurement says
SKIN_COLOUR_RANGE = 45.0  # Lab distance from the face tone at which a pixel stops counting as skin


def _skin_tone(image: np.ndarray, face: Face) -> np.ndarray | None:
    """Average Lab colour of the inner face (cheeks, nose, forehead), avoiding hair, glasses rims and background."""
    x, y, w, h = face.box
    mask = np.zeros(image.shape[:2], np.uint8)
    cv2.ellipse(mask, (int(x + w / 2), int(y + h * 0.55)), (max(2, int(w * 0.30)), max(2, int(h * 0.32))), face.tilt, 0, 360, 255, -1)
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
    pixels = lab[mask > 0]
    if len(pixels) < 50:
        return None
    lightness = pixels[:, 0]
    low, high = np.percentile(lightness, [15, 90])  # drop shadows, eyes, beard and specular highlights
    keep = pixels[(lightness >= low) & (lightness <= high)]
    return keep.mean(axis=0) if len(keep) >= 30 else None


def match_face_tone(reference: bytes, edited: bytes, output_mime: str = "image/png") -> bytes | None:
    """Give the head in `edited` the skin tone the face had in `reference`, the same picture before a face
    pass. The first drawing lights the face like the neck, arms and hands; a face pass copies the colour of
    the person's own photo, taken in different light, so the face no longer matches the body. Only the
    average colour moves: every feature, shadow and texture of the edited face is kept. None when no change
    is needed or the faces cannot be found."""
    try:
        before = cv2.imdecode(np.frombuffer(reference, np.uint8), cv2.IMREAD_COLOR)
        after = cv2.imdecode(np.frombuffer(edited, np.uint8), cv2.IMREAD_COLOR)
        if before is None or after is None:
            return None
        face_before, face_after = _detect(before), _detect(after)
        if face_before is None or face_after is None:
            return None
        tone_before, tone_after = _skin_tone(before, face_before), _skin_tone(after, face_after)
        if tone_before is None or tone_after is None:
            return None
        shift = tone_before - tone_after
        distance = float(np.linalg.norm(shift))
        if distance < TONE_SHIFT_LIMIT:
            return None
        if distance > TONE_MAX_SHIFT:
            shift *= TONE_MAX_SHIFT / distance
        lab = cv2.cvtColor(after, cv2.COLOR_BGR2LAB).astype(np.float32)
        # Move only skin: weight each pixel in the head area by how close its colour is to the face's own
        # tone, so hair, glasses, beard and the background around the head keep their colour (no halo).
        closeness = np.linalg.norm((lab - tone_after[None, None, :]) * np.array([0.5, 1.0, 1.0], np.float32), axis=2)
        skin = np.sqrt(np.clip(1.0 - closeness / SKIN_COLOUR_RANGE, 0.0, 1.0))
        skin = cv2.GaussianBlur(skin, (0, 0), max(1.0, float(face_after.box[2]) * 0.03))
        mask = _head_mask(face_after, after.shape[:2]) * skin
        lab += mask[..., None] * shift[None, None, :]
        corrected = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
        log.info("Face tone matched to the body: shift L%+.1f a%+.1f b%+.1f", *shift)
        extension = ".jpg" if output_mime == "image/jpeg" else ".webp" if output_mime == "image/webp" else ".png"
        params = [cv2.IMWRITE_JPEG_QUALITY, 95] if extension == ".jpg" else []
        ok, encoded = cv2.imencode(extension, corrected, params)
        return encoded.tobytes() if ok else None
    except cv2.error:
        log.exception("Face tone matching failed")
        return None
