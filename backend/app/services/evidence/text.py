"""Text from PDF and TXT evidence, with locators the timeline can cite ("page 3", "message 2").
Ingest stores each one as an `evidence_page` row.

PDF parsing is bounded: at most MAX_PAGES pages, MAX_CHARS characters and PDF_SECONDS of
work (checked between pages). Pages that share one content stream are parsed once. Ingest
calls read_pdf_isolated, which runs read_pdf in a child process and kills it at
PDF_HARD_SECONDS, since one hostile page could run past the between-pages check."""

import io
import logging
import multiprocessing
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from pypdf import PdfReader
from pypdf.generic import ArrayObject, IndirectObject

from app.services.evidence import sniff

MAX_PAGES = 300
MAX_CHARS = 1_000_000  # per document
MAX_MESSAGES = 5000
PDF_SECONDS = 10.0
PDF_HARD_SECONDS = PDF_SECONDS + 5.0  # plus starting the child process and returning the text
PDF_CHILD_MEMORY = 2 * 1024**3  # address space of the PDF child, where the OS can cap it
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")

log = logging.getLogger(__name__)

Pages = list[dict[str, str]]


class PdfTimeout(Exception):
    """The child process reading a PDF was killed at the hard time limit."""


@dataclass(frozen=True)
class PdfText:
    pages: Pages
    software: str | None  # Producer or Creator, shown like a photo's software
    complete: bool  # False when a limit stopped extraction early
    # Scanned pages, for OCR: locator ("page 3") -> the page's image as PNG, made here in the
    # child from pixels only, grey, at most SCAN_SIDE on its long side.
    scans: dict[str, bytes] = field(default_factory=dict)


SCAN_MIN_CHARS = 20  # a page with less text than this is treated as a scan
MAX_SCANS = 10  # scanned pages per PDF that get OCR
SCAN_SIDE = 2000
SCAN_MIN_SIDE = 300  # smaller images are logos or stamps, not a scanned page
SCAN_MAX_PIXELS = 60_000_000  # the photo limit (strip.MAX_PIXELS)


def _scan_png(page: Any) -> bytes | None:
    """The page's largest image as a grey PNG, or None. Any decoder error skips the page: its
    (empty) text layer is kept and the rest of the PDF is read as before."""
    try:
        images = [f.image for f in page.images]
    except Exception as exc:  # an image filter pypdf can't decode (JBIG2, ...)
        log.debug("scan skipped: %s", type(exc).__name__)
        return None
    images = [im for im in images if im is not None and min(im.size) >= SCAN_MIN_SIDE]
    if not images:
        return None
    img = max(images, key=lambda im: im.size[0] * im.size[1])
    if img.size[0] * img.size[1] > SCAN_MAX_PIXELS:
        return None
    grey = img.convert("L")
    grey.thumbnail((SCAN_SIDE, SCAN_SIDE))
    out = io.BytesIO()
    grey.save(out, format="PNG")
    return out.getvalue()


def read_pdf(data: bytes, scans: bool = False) -> PdfText:
    """Text per page. With `scans`, also the images of up to MAX_SCANS pages that have almost
    no text, for OCR (uploads only; the legal source check doesn't need them)."""
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF is password-protected")
    deadline = time.monotonic() + PDF_SECONDS
    seen: dict[tuple[int, ...], str] = {}
    pages: Pages = []
    found: dict[str, bytes] = {}
    tried = 0  # pages whose images were decoded; failures count, so MAX_SCANS bounds the work
    budget = MAX_CHARS
    complete = len(reader.pages) <= MAX_PAGES
    for number, page in enumerate(reader.pages[:MAX_PAGES], start=1):
        if budget <= 0 or time.monotonic() > deadline:
            complete = False
            break
        key = _contents_key(page.raw_get("/Contents") if "/Contents" in page else None)
        text = seen.get(key) if key else None
        if text is None:
            text = _clean(page.extract_text() or "")
            if key:
                seen[key] = text
        text = text[:budget]
        budget -= len(text)
        locator = f"page {number}"
        pages.append({"locator": locator, "text": text})
        if scans and tried < MAX_SCANS and len("".join(text.split())) < SCAN_MIN_CHARS:
            tried += 1
            png = _scan_png(page)
            if png is not None:
                found[locator] = png
    info = reader.metadata
    producer = (info.producer or info.creator) if info is not None else None
    software = _clean(str(producer)).strip()[:100] if producer else ""
    return PdfText(pages=pages, software=software or None, complete=complete, scans=found)


def read_pdf_isolated(
    data: bytes, timeout: float | None = None, scans: bool = False
) -> PdfText:
    """read_pdf in a child process that is killed after `timeout` (PDF_HARD_SECONDS) seconds.
    Never a plain fork: the server has threads, and a forked child could inherit a held lock.
    On Linux and macOS a fork server (one thread, this module preloaded) forks each child in
    milliseconds; Windows only has spawn, a fresh interpreter each time (about 0.6 s)."""
    timeout = PDF_HARD_SECONDS if timeout is None else timeout
    if sys.platform == "win32":
        ctx = multiprocessing.get_context("spawn")
    else:
        ctx = multiprocessing.get_context("forkserver")
        ctx.set_forkserver_preload([__name__])  # used when the fork server first starts
    result, child_end = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_read_pdf_child, args=(data, child_end, scans), daemon=True)
    proc.start()
    child_end.close()  # the parent keeps only its end, so a dead child reads as EOF
    try:
        if not result.poll(timeout):
            raise PdfTimeout(f"PDF text took longer than {timeout:.0f} s")
        ok, value = result.recv()
    except EOFError as exc:
        raise RuntimeError("the PDF reader exited without a result") from exc
    finally:
        result.close()
        if proc.is_alive():
            proc.kill()
        proc.join()
    if not ok:
        raise ValueError(value)
    return value


def limit_child_resources() -> None:
    """Cap the PDF child's memory and CPU where the OS allows it (not Windows), so a hostile
    PDF fails in its own process (MemoryError, or killed) instead of starving the server.
    A limit the OS refuses (setrlimit is unreliable for the address space on macOS) is skipped,
    since the hard timeout still bounds the child; the PDF is read either way."""
    if sys.platform == "win32":  # no setrlimit: the hard timeout is the only limit
        return
    import resource

    for name, limit, value in (("RLIMIT_AS", resource.RLIMIT_AS, PDF_CHILD_MEMORY),
                               ("RLIMIT_CPU", resource.RLIMIT_CPU, int(PDF_HARD_SECONDS) + 5)):
        try:
            _, hard = resource.getrlimit(limit)
            resource.setrlimit(limit, (value if hard == resource.RLIM_INFINITY
                                       else min(value, hard), hard))
        except (OSError, ValueError) as exc:
            log.debug("PDF child: %s not set (%s)", name, type(exc).__name__)  # type only


class _PipeEnd(Protocol):
    """The sending end of multiprocessing.Pipe(). Typeshed calls it Connection on POSIX and
    PipeConnection on Windows, and PipeConnection doesn't exist at runtime off Windows, so
    neither class can be named here."""

    def send(self, obj: Any, /) -> None: ...
    def close(self) -> None: ...


def _read_pdf_child(data: bytes, conn: _PipeEnd, scans: bool = False) -> None:
    try:
        limit_child_resources()
        conn.send((True, read_pdf(data, scans=scans)))
    except Exception as exc:  # any parser error: the parent marks the file failed
        conn.send((False, type(exc).__name__))
    finally:
        conn.close()


def read_txt(data: bytes) -> Pages:
    """One locator per non-empty line ("message 1", "message 2", ...): chat exports put one
    message on each line, and the timeline cites messages by number."""
    # The encodings sniff accepts; anything else as UTF-8, with control characters cleaned.
    decoded = sniff.decode_text(data)
    text = _clean(data.decode("utf-8-sig") if decoded is None else decoded)[:MAX_CHARS]
    # Newlines only: splitlines() would also break on form feeds and U+2028 inside a message.
    lines = [line.strip() for line in re.split(r"\r\n|\r|\n", text) if line.strip()]
    return [{"locator": f"message {n}", "text": line}
            for n, line in enumerate(lines[:MAX_MESSAGES], start=1)]


def _contents_key(raw: object) -> tuple[int, ...]:
    """Identity of a page's content stream(s), so a stream shared by many pages is parsed
    once. Empty when the contents are inline (no reuse possible)."""
    if isinstance(raw, IndirectObject):
        return (raw.idnum, raw.generation)
    if isinstance(raw, ArrayObject) and all(isinstance(r, IndirectObject) for r in raw):
        return tuple(n for r in raw for n in (r.idnum, r.generation))
    return ()


def _clean(text: str) -> str:
    return _CONTROL.sub("", text)
