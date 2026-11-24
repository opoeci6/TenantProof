"""The perceptual fingerprint that backs the "possible duplicate" note (#127)."""

import io

from PIL import Image, ImageDraw

from app.services.evidence import fingerprint


def image_bytes(draw) -> bytes:
    image = Image.new("L", (160, 120), 255)
    draw(ImageDraw.Draw(image))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def a_stripe() -> bytes:
    return image_bytes(lambda d: d.rectangle((20, 20, 120, 90), fill=40))


def test_the_same_picture_has_distance_zero() -> None:
    first, second = a_stripe(), a_stripe()
    a, b = fingerprint.dhash_bytes(first), fingerprint.dhash_bytes(second)
    assert a is not None and b is not None
    assert fingerprint.distance(a, b) == 0
    assert fingerprint.looks_the_same(a, b)


def test_a_recompressed_or_resized_copy_stays_close() -> None:
    original = a_stripe()
    with Image.open(io.BytesIO(original)) as img:
        small = img.resize((80, 60))
        out = io.BytesIO()
        small.save(out, format="JPEG", quality=60)
    a, b = fingerprint.dhash_bytes(original), fingerprint.dhash_bytes(out.getvalue())
    assert a is not None and b is not None
    assert fingerprint.distance(a, b) <= fingerprint.MAX_DISTANCE


def test_two_different_pictures_are_not_called_the_same() -> None:
    one = a_stripe()
    two = image_bytes(lambda d: d.ellipse((30, 10, 130, 110), fill=10))
    a, b = fingerprint.dhash_bytes(one), fingerprint.dhash_bytes(two)
    assert not fingerprint.looks_the_same(a, b)


def test_an_undecodable_file_has_no_fingerprint() -> None:
    assert fingerprint.dhash_bytes(b"not an image") is None
    assert not fingerprint.looks_the_same(None, "0000000000000000")
