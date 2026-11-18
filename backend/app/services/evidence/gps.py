"""Remove location data from the copy of an image that goes in the filing package.

The stored original is never touched; only the bytes handed to the package change. Nothing is
re-encoded: the JPEG or PNG is copied as is and only its metadata is rewritten, so the pixels,
the capture time (DateTimeOriginal) and the orientation stay exactly as they were.

What goes: the GPS IFD of the EXIF block, and an XMP packet that carries GPS tags. What stays:
everything else in the EXIF block. Not covered: location written in other places (a maker note,
a legacy "Raw profile type exif" PNG text chunk), which this module does not read.
"""

import io
import struct
import zlib

from PIL import Image

from app.services.evidence import sniff

GPS_IFD = 0x8825
_EXIF_HEADER = b"Exif\x00\x00"
_XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_JPEG_SEGMENT = 0xFFFF - 2


def without_gps(data: bytes, mime: str) -> tuple[bytes, bool]:
    """(bytes, removed). `removed` is False, and `data` is returned unchanged, when the image
    carried no location data. Raises ValueError for a file this module cannot walk, so the
    caller can leave the image out rather than file it with its location in it."""
    if mime == sniff.JPEG:
        return _jpeg(data)
    if mime == sniff.PNG:
        return _png(data)
    raise ValueError(f"not a JPEG or PNG: {mime}")


def exif_has_gps(data: bytes) -> bool:
    """True when a decodable image (JPEG, PNG or HEIC) carries a GPS IFD in its EXIF."""
    with Image.open(io.BytesIO(data)) as img:
        return GPS_IFD in img.getexif()


def _clean_exif(exif_bytes: bytes) -> tuple[bytes, bool]:
    """The EXIF block (with or without the `Exif\\0\\0` header) minus its GPS IFD."""
    exif = Image.Exif()
    exif.load(exif_bytes)
    if GPS_IFD not in exif:
        return exif_bytes, False
    del exif[GPS_IFD]
    return exif.tobytes(), True


def _jpeg(data: bytes) -> tuple[bytes, bool]:
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    out = bytearray(data[:2])
    removed = False
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            raise ValueError("bad JPEG marker")
        marker = data[i + 1] if i + 1 < len(data) else 0
        if marker == 0xFF:  # fill byte
            out.append(0xFF)
            i += 1
            continue
        if marker in (0xDA, 0xD9):  # start of scan or end of image: the rest is image data
            out += data[i:]
            return bytes(out), removed
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # markers without a length
            out += data[i : i + 2]
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        end = i + 2 + length
        if length < 2 or end > len(data):
            raise ValueError("bad JPEG segment length")
        payload = data[i + 4 : end]
        if marker == 0xE1 and payload.startswith(_EXIF_HEADER):
            new, did = _clean_exif(payload)
            removed = removed or did
            if len(new) > _MAX_JPEG_SEGMENT:
                raise ValueError("EXIF block too large")
            out += b"\xff\xe1" + struct.pack(">H", len(new) + 2) + new
        elif marker == 0xE1 and payload.startswith(_XMP_HEADER) and b"GPS" in payload:
            removed = True  # drop the whole packet: XMP is not rewritten
        else:
            out += data[i:end]
        i = end
    raise ValueError("JPEG ends before its image data")


def _png(data: bytes) -> tuple[bytes, bool]:
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG")
    out = bytearray(_PNG_SIGNATURE)
    removed = False
    i = len(_PNG_SIGNATURE)
    while i + 8 <= len(data):
        length = struct.unpack(">I", data[i : i + 4])[0]
        kind = data[i + 4 : i + 8]
        end = i + 12 + length
        if end > len(data):
            raise ValueError("bad PNG chunk length")
        body = data[i + 8 : i + 8 + length]
        if kind == b"eXIf":
            new, did = _clean_exif(body)
            if did:
                removed = True
                new = new.removeprefix(_EXIF_HEADER)  # a PNG eXIf chunk has no header
                out += _png_chunk(kind, new)
            else:
                out += data[i:end]
        elif kind == b"iTXt" and body.startswith(b"XML:com.adobe.xmp\x00") and b"GPS" in body:
            removed = True
        else:
            out += data[i:end]
        i = end
        if kind == b"IEND":
            return bytes(out) + data[i:], removed
    raise ValueError("PNG ends before IEND")


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)
