"""Decide a file's type from its bytes, never from its name or the Content-Type header.

Pure Python on purpose: python-magic needs the native libmagic library, which Windows machines
don't have without a system-wide install (AGENTS.md forbids those). Five allowed types need
only their signatures."""

import unicodedata
from typing import BinaryIO

JPEG = "image/jpeg"
PNG = "image/png"
HEIC = "image/heic"
PDF = "application/pdf"
TEXT = "text/plain"

ALLOWED = (JPEG, PNG, HEIC, PDF, TEXT)
ALLOWED_LABEL = "JPG, PNG, HEIC, PDF or TXT"

HEAD = 16  # enough for every signature below
# ISO base media "ftyp" major brands used by HEIC/HEIF stills from phones.
_HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}
# Control characters allowed in a text file: tab, line feed, form feed, carriage return.
_TEXT_CONTROLS = {0x09, 0x0A, 0x0C, 0x0D}


def sniff(data: bytes) -> str | None:
    """Return the MIME type if `data` is one of the allowed types, else None."""
    return _by_signature(data[:HEAD]) or (TEXT if _is_text(data) else None)


def sniff_file(source: BinaryIO, max_bytes: int) -> str | None:
    """Like sniff(), reading only the head unless the file can only be text. Rewinds."""
    source.seek(0)
    mime = _by_signature(source.read(HEAD))
    if mime is None:
        source.seek(0)
        mime = TEXT if _is_text(source.read(max_bytes)) else None
    source.seek(0)
    return mime


def _by_signature(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head.startswith(b"%PDF-"):
        return PDF
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in _HEIC_BRANDS:
        return HEIC
    return None


def _is_text(data: bytes) -> bool:
    return decode_text(data) is not None


def looks_like_text(data: bytes) -> bool:
    """True when the bytes are printable in some 8-bit encoding: no NUL and no control
    character other than tab, line feed, form feed and carriage return. A file that looks like
    this and that `decode_text` refuses is text in an encoding we can't read (see there), which
    needs a different 415 reason than a binary file. It is still refused."""
    if not data:
        return False
    return all((byte >= 0x20 and byte != 0x7F) or byte in _TEXT_CONTROLS for byte in data)


def looks_like_text_file(source: BinaryIO, max_bytes: int) -> bool:
    """looks_like_text() over a file, reading at most max_bytes. Rewinds."""
    source.seek(0)
    looks = looks_like_text(source.read(max_bytes))
    source.seek(0)
    return looks


# Byte-order marks: UTF-8 from some editors, UTF-16 from Windows "Unicode" exports.
_BOMS = ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))
_LENIENT = ("utf-16", "gb18030")


def decode_text(data: bytes) -> str | None:
    """A text file's contents, or None if it isn't text. UTF-16 only with a byte-order mark;
    without one, UTF-8, then GB18030 (GBK, which Chinese chat exports often use).
    Binary control characters mean it isn't text, whatever decoded. UTF-16 and GB18030 decode
    almost any bytes, so for them private-use and unassigned characters (what binary data
    turns into) mean it isn't text either. Empty files are not evidence.

    The GB18030 result must also fit GB2312 (simplified Chinese): Windows-1252 English or
    French ("don't" with a curly apostrophe, "propriétaire") and Shift-JIS decode as GBK too,
    into Chinese characters, and would be filed garbled. Those are refused (415, re-save as
    UTF-8), as are GBK files with characters outside GB2312. Korean EUC-KR still passes.

    Big5 is refused as well. A Big5 file decodes as GB18030 into private-use characters, and
    nothing in the bytes tells the two code pages apart (both allow trail bytes from 0x40), so
    a guess would file traditional Chinese as different characters. Every refused encoding gets
    the same 415 reason: re-save the file as UTF-8, which always reads back exactly."""
    if not data:
        return None
    codecs = next(((codec,) for bom, codec in _BOMS if data.startswith(bom)),
                  ("utf-8", "gb18030"))
    for codec in codecs:
        try:
            text = data.decode(codec)
            if codec == "gb18030":
                text.encode("gb2312")
        except UnicodeError:
            continue
        return text if text and _plain(text, codec in _LENIENT) else None
    return None


def _plain(text: str, lenient_codec: bool) -> bool:
    for c in text:
        if ord(c) < 0x20 and ord(c) not in _TEXT_CONTROLS or c == "\x7f":
            return False
        if lenient_codec and ord(c) > 0x7F and _binary_like(c):
            return False
    return True


def _binary_like(c: str) -> bool:
    """Private use and surrogates anywhere; unassigned only in the BMP, since emoji newer than
    Python's Unicode tables are unassigned to it, and binary data almost never decodes past
    the BMP."""
    category = unicodedata.category(c)
    return category in ("Co", "Cs") or (category == "Cn" and ord(c) <= 0xFFFF)
