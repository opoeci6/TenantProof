"""Re-encode a photo with no metadata. This copy is the only one that may go to the model.

Only pixels are carried over: EXIF (including GPS), XMP, ICC profiles and PNG text chunks are
all dropped. The EXIF orientation is applied to the pixels first, so the copy looks the same.
The copy is at most MAX_SIDE pixels on its long side; the model needs no more."""

import io

from PIL import Image, ImageOps

JPEG_QUALITY = 90
MAX_SIDE = 4096
MAX_PIXELS = 60_000_000  # decoded size we accept (a 48 MP phone photo fits); larger = failed


class TooLarge(ValueError):
    pass


def strip_image(img: Image.Image, source_mime: str) -> tuple[bytes, str]:
    """Return (bytes, mime). PNG stays PNG (lossless); JPEG and HEIC become JPEG.
    Changes `img` in place (orientation applied); the caller reads metadata first."""
    width, height = img.size
    if width * height > MAX_PIXELS:
        raise TooLarge(f"{width}x{height} is over the pixel limit")
    if img.format == "JPEG":
        img.draft("RGB", (MAX_SIDE, MAX_SIDE))  # decode at a reduced scale when it can
    ImageOps.exif_transpose(img, in_place=True)
    img.thumbnail((MAX_SIDE, MAX_SIDE))  # in place, before any copy is made
    keep_png = source_mime == "image/png"
    has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
    mode = "RGBA" if keep_png and has_alpha else "RGB"
    # A fresh image holds pixels only; nothing from the source's info dict can leak through.
    clean = Image.new(mode, img.size)
    clean.paste(img if img.mode == mode else img.convert(mode))
    out = io.BytesIO()
    if keep_png:
        clean.save(out, format="PNG", optimize=True)
        return out.getvalue(), "image/png"
    clean.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue(), "image/jpeg"
