"""Read the evidence-log metadata from a photo: capture time, GPS present, device, software.

Only these four facts are kept. The GPS coordinates themselves are never read or stored."""

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from PIL import Image

# EXIF tag ids (TIFF/EXIF 2.32).
_MAKE, _MODEL, _SOFTWARE, _DATETIME = 0x010F, 0x0110, 0x0131, 0x0132
_EXIF_IFD, _GPS_IFD = 0x8769, 0x8825
_DATETIME_ORIGINAL, _OFFSET_TIME_ORIGINAL = 0x9003, 0x9011

_OFFSET = re.compile(r"[+-]\d{2}:\d{2}")
# The Software tag is written by camera apps and phone firmware too, not only by editors.
# `software` reports editing software, so these capture-side values are dropped:
# iOS writes its version ("17.5"), Samsung its firmware ("G998BXXU5CVDD"), Pixel "HDR+ 1.0...",
# and third-party camera apps their own name.
_CAPTURE_SOFTWARE = re.compile(
    r"\d+(\.\d+)*"
    r"|i(OS|PadOS) \d.*"
    r"|[A-Z]\d{3,4}[A-Z0-9]{6,}"
    r"|HDR\+.*"
    r"|(ProCamera|Halide|Camera\+|ProShot|FiLMiC Pro|Blackmagic Camera|Open Camera"
    r"|Moment|Obscura|ProCam)\b.*"
    r"|Camera( \d.*)?",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_TEXT = 100


@dataclass(frozen=True)
class PhotoMeta:
    captured_at: str | None
    gps_present: bool
    device: str | None
    software: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


NO_META = PhotoMeta(captured_at=None, gps_present=False, device=None, software=None)


def read_meta(img: Image.Image) -> PhotoMeta:
    exif = img.getexif()
    if not exif:
        return NO_META
    sub = exif.get_ifd(_EXIF_IFD)
    gps = exif.get_ifd(_GPS_IFD)
    return PhotoMeta(
        captured_at=_captured_at(sub.get(_DATETIME_ORIGINAL) or exif.get(_DATETIME),
                                 sub.get(_OFFSET_TIME_ORIGINAL)),
        gps_present=bool(gps),
        device=_device(exif.get(_MAKE), exif.get(_MODEL)),
        software=_editor(exif.get(_SOFTWARE)),
    )


def _editor(value: object) -> str | None:
    """The Software tag only when it names an editor (Lightroom, Snapseed, Photoshop...)."""
    text = _clean(value)
    return None if text is None or _CAPTURE_SOFTWARE.fullmatch(text) else text


def _clean(value: object) -> str | None:
    """EXIF strings are untrusted: drop control characters and cap the length."""
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub("", value).strip()
    return text[:MAX_TEXT] or None


def _device(make: object, model: object) -> str | None:
    make_s, model_s = _clean(make), _clean(model)
    if make_s and model_s and model_s.lower().startswith(make_s.lower()):
        return model_s  # "Canon" + "Canon EOS R6" -> "Canon EOS R6"
    joined = " ".join(p for p in (make_s, model_s) if p)
    return joined[:MAX_TEXT] or None


def _captured_at(value: object, offset: object) -> str | None:
    """EXIF "YYYY:MM:DD HH:MM:SS" to ISO 8601. The offset is added only when the photo
    recorded one; otherwise the time is the camera's local time with no zone."""
    text = _clean(value)
    if text is None:
        return None
    try:
        stamp = datetime.strptime(text, "%Y:%m:%d %H:%M:%S")  # zone, if any, is a separate tag
    except ValueError:
        return None
    iso = stamp.isoformat()
    zone = _clean(offset)
    return iso + zone if zone and _OFFSET.fullmatch(zone) else iso


PHOTO_LOCATOR = "photo 1"


def photo_page(captured_at: str | None) -> dict[str, str]:
    """A photo's first `evidence_page`, written by code, never read from the file: a model can
    cite it for the photo's capture date (the agent never sees the image). It takes only the
    capture time, which `_captured_at` has already parsed to digits, so GPS, device and software
    cannot reach it. With OCR on, text found in the image is a second page (ingest)."""
    if captured_at:
        text = f"Photo file. Capture time (EXIF): {captured_at}."
    else:
        text = "Photo file. No capture time in the file."
    return {"locator": PHOTO_LOCATOR, "text": text}

