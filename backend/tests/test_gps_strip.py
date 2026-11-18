"""`services/evidence/gps.py`: the package copy of an image loses its GPS data and nothing else.
No database, no network."""

import io
import struct

import pytest
from PIL import Image

from app.services.evidence import gps, sniff

GPS_IFD = 0x8825
EXIF_IFD = 0x8769


def _jpeg(*, gps_tags: bool) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = 3
    exif.get_ifd(EXIF_IFD)[0x9003] = "2026:08:14 21:03:12"
    if gps_tags:
        g = exif.get_ifd(GPS_IFD)
        g[1], g[2], g[3], g[4] = "N", (45.0, 25.0, 0.0), "W", (63.0, 35.0, 0.0)
    out = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 10, 10)).save(out, "JPEG", exif=exif.tobytes())
    return out.getvalue()


def _with_xmp(data: bytes, xml: bytes) -> bytes:
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + xml
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    return data[:2] + segment + data[2:]


def _exif(data: bytes) -> Image.Exif:
    with Image.open(io.BytesIO(data)) as img:
        return img.getexif()


def test_the_gps_ifd_goes_and_the_capture_time_and_orientation_stay() -> None:
    cleaned, removed = gps.without_gps(_jpeg(gps_tags=True), sniff.JPEG)

    assert removed is True
    exif = _exif(cleaned)
    assert GPS_IFD not in exif
    assert exif.get_ifd(EXIF_IFD)[0x9003] == "2026:08:14 21:03:12"
    assert exif[0x0112] == 3


def test_a_file_with_no_gps_comes_back_unchanged() -> None:
    data = _jpeg(gps_tags=False)
    cleaned, removed = gps.without_gps(data, sniff.JPEG)
    assert removed is False and cleaned == data


def test_the_image_data_after_the_metadata_is_not_touched() -> None:
    data = _jpeg(gps_tags=True)
    cleaned, _ = gps.without_gps(data, sniff.JPEG)
    assert cleaned[cleaned.index(b"\xff\xda"):] == data[data.index(b"\xff\xda"):]


def test_an_xmp_packet_with_gps_tags_is_dropped() -> None:
    data = _with_xmp(_jpeg(gps_tags=False), b"<x:xmpmeta exif:GPSLatitude='45,25N'/>")
    cleaned, removed = gps.without_gps(data, sniff.JPEG)
    assert removed is True
    assert b"GPSLatitude" not in cleaned


def test_an_xmp_packet_without_gps_is_kept() -> None:
    data = _with_xmp(_jpeg(gps_tags=False), b"<x:xmpmeta dc:title='ceiling'/>")
    cleaned, removed = gps.without_gps(data, sniff.JPEG)
    assert removed is False and cleaned == data


@pytest.mark.parametrize("bad", [b"", b"not an image", b"\xff\xd8\xff\xe1\x00\x10Exif"])
def test_a_file_that_cannot_be_walked_raises(bad: bytes) -> None:
    with pytest.raises(ValueError):
        gps.without_gps(bad, sniff.JPEG)


def test_a_mime_that_is_not_jpeg_or_png_raises() -> None:
    with pytest.raises(ValueError):
        gps.without_gps(b"%PDF-1.4", sniff.PDF)


def test_exif_has_gps_reports_both_ways() -> None:
    assert gps.exif_has_gps(_jpeg(gps_tags=True)) is True
    assert gps.exif_has_gps(_jpeg(gps_tags=False)) is False
