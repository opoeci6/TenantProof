"""Text read from images on this server (OCR), so the agent can cite what a photo of a notice or a
screenshot of a message says. The model still never sees an image: it reads this text, like the
text of a PDF or a chat export.

RapidOCR (Apache-2.0) runs locally with the PP-OCR models bundled in the wheel; nothing is
downloaded and nothing leaves the server. It reads English, Chinese, French and Spanish well. It
does not read Cyrillic or Devanagari: on Russian or Hindi text it returns punctuation or
low-confidence guesses, which the filters below drop, so a misread never becomes evidence.

Callers pass an image that is already decoded, upright and size-checked (the stripped photo, or a
scanned PDF page re-encoded by the PDF child process); this module never parses a file. It still
refuses thin shapes: RapidOCR scales the short side up (to 30 px, then 736 px for the detector)
without capping the long side, so a 2000x1 image would ask for a 900-megapixel buffer."""

import logging
import re
import threading
import time
from functools import cache
from typing import Any

from PIL import Image

log = logging.getLogger(__name__)

MIN_SCORE = 0.9  # misreads of scripts the model doesn't know score 0.55 to 0.87; real text 0.97+
MIN_LETTERS = 2  # a line of punctuation or one stray character is noise, not text
MAX_SIDE = 2000  # long side for OCR; phone text stays readable and a check takes ~2 s
MIN_SIDE = 32  # short side after shrinking; thinner images are lines or bars, not text
MAX_RATIO = 20  # long side / short side; with MAX_SIDE the detector stays under ~11 MP
MAX_LINES = 300
MAX_CHARS = 20_000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
_lock = threading.Lock()  # one OCR at a time: bounds CPU use and the engine's memory


class Busy(Exception):
    """Another request's OCR held the engine longer than this request could wait."""


@cache
def _engine() -> Any:
    from rapidocr import RapidOCR  # imported on first use: loading the models takes ~1 s

    return RapidOCR(params={"Global.log_level": "warning"})


def readable_shape(width: int, height: int) -> bool:
    short, long = min(width, height), max(width, height)
    return short >= MIN_SIDE and long <= MAX_RATIO * short


def read_text(img: Image.Image, wait_s: float = 60.0) -> str:
    """The lines of text in the image, top to bottom, one per line. Empty when there is none,
    none the model is sure of, or the image is too thin to read safely. Raises Busy when the
    engine stays taken for `wait_s` seconds."""
    rgb = img.convert("RGB")  # a copy: the caller's image is not changed
    rgb.thumbnail((MAX_SIDE, MAX_SIDE))
    if not readable_shape(*rgb.size):
        return ""
    # TIMEOUT_MAX: a huge or infinite OCR_REQUEST_SECONDS must not raise OverflowError here.
    if not _lock.acquire(timeout=min(max(wait_s, 0.0), threading.TIMEOUT_MAX)):
        raise Busy
    try:
        result: Any = _engine()(rgb)
    finally:
        _lock.release()
    lines: list[str] = []
    for raw, score in zip(result.txts or (), result.scores or (), strict=False):
        found = str(raw).strip()
        if float(score) >= MIN_SCORE and sum(ch.isalnum() for ch in found) >= MIN_LETTERS:
            lines.append(found)
    return _CONTROL.sub("", "\n".join(lines[:MAX_LINES]))[:MAX_CHARS]


class Budget:
    """Seconds of OCR one upload request may use, waiting for the engine included. 20 photos
    at ~2 s each would hold the request (and the tenant's phone) for most of a minute, and a
    proxy may cut it off. Images after the budget is spent are kept without their text, and
    the evidence says so."""

    def __init__(self, seconds: float) -> None:
        self._limit = seconds
        self._spent = 0.0

    def left(self) -> bool:
        return self._spent < self._limit

    def remaining(self) -> float:
        return max(self._limit - self._spent, 0.0)

    def read(self, img: Image.Image) -> str:
        """read_text, timed against this budget. Raises Busy when the wait uses it up."""
        start = time.monotonic()
        try:
            return read_text(img, wait_s=self.remaining())
        finally:
            self._spent += time.monotonic() - start
