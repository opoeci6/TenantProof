"""Build demo/local/wall_stain.jpg from the downloaded stock photo (see demo/README.md).

    uv run --directory backend python ../demo/prepare_photo.py /path/to/downloaded.jpg

Resizes to at most 1600 px on the long side (never enlarges), drops every piece of metadata the
download carries (EXIF, GPS, device, colour profile, comments), and sets one EXIF field by hand:
DateTimeOriginal = 2026:08:14 21:03:12. The result goes in demo/local/, which git ignores."""

import sys
from pathlib import Path

from PIL import Image

MAX_SIDE = 1600
CAPTURED = "2026:08:14 21:03:12"  # set by hand: the photo is not from a real tenant
OUT = Path(__file__).resolve().parent / "local" / "wall_stain.jpg"


def main(source: str) -> None:
    with Image.open(source) as im:
        pixels = im.convert("RGB")  # a new image: none of the download's metadata comes along
    pixels.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)  # keeps the aspect ratio
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = CAPTURED  # Exif sub-IFD: DateTimeOriginal
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pixels.save(OUT, "JPEG", quality=88, exif=exif)
    print(f"wrote {OUT} ({pixels.width}x{pixels.height})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: prepare_photo.py /path/to/downloaded.jpg")
    main(sys.argv[1])
