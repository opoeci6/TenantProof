"""A small perceptual fingerprint for an image, used to notice when two uploads are the same
picture in different wrappers (a phone photo and a PDF made from it). It is a difference hash:
shrink to greyscale, then record which of each pair of neighbouring pixels is darker. Two copies
of one picture - recompressed, wrapped in a PDF, or saved at another size - land within a few
bits of each other; two different photos do not.

This decides only what to *say* in the evidence list ("possible duplicate of E-04"). It never
merges, drops or reorders evidence."""

import io

from PIL import Image

SIDE = 8  # 8x8 comparisons -> 64 bits
MAX_DISTANCE = 6  # enough for recompression, too small for two different photos


def dhash(image: Image.Image) -> str:
    """64-bit difference hash as 16 hex characters."""
    small = image.convert("L").resize((SIDE + 1, SIDE), Image.Resampling.LANCZOS)
    pixels = small.tobytes()  # 8-bit greyscale, one byte per pixel, row by row
    bits = 0
    for y in range(SIDE):
        row = y * (SIDE + 1)
        for x in range(SIDE):
            bits = (bits << 1) | (1 if pixels[row + x] < pixels[row + x + 1] else 0)
    return f"{bits:016x}"


def dhash_bytes(data: bytes) -> str | None:
    """The hash of an encoded image, or None when it can't be decoded (never an upload error)."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            return dhash(image)
    except Exception:  # any decoder error: no fingerprint, keep the file
        return None


def distance(a: str, b: str) -> int:
    """How many bits two hashes differ by; 0 means identical pixels at this size."""
    return (int(a, 16) ^ int(b, 16)).bit_count()


def looks_the_same(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and distance(a, b) <= MAX_DISTANCE
