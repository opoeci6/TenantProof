"""Text from PDF and TXT evidence, with locators the timeline can cite ("page 3", "message 2",
"line 4" in a TXT that is not a chat). Ingest stores each one as an `evidence_page` row.

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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

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


TxtKind = Literal["messages", "letter"]
MIN_STAMPED = 3  # this many timestamped lines make a chat, however long its messages are
_MONTH = (
    r"(?i:January|Jan|February|Feb|March|Mar|April|Apr|May|June|Jun|July|Jul|August|Aug"
    r"|September|Sept|Sep|October|Oct|November|Nov|December|Dec"
    r"|janvier|janv|février|fevrier|févr|fevr|mars|avril|avr|mai|juin|juillet|juil"
    r"|août|aout|septembre|octobre|novembre|décembre|decembre|déc|dec)\b\.?"
)
_DATE = (
    r"[0-9]{4}[-/.年][0-9]{1,2}[-/.月][0-9]{1,2}日?"  # 2026-08-14, 2026/8/14, 2026年8月14日
    r"|[0-9]{1,2}[-/.][0-9]{1,2}[-/.][0-9]{2,4}"  # 14/08/2026, 8/14/26, 14.08.2026
    rf"|{_MONTH}\s+[0-9]{{1,2}}(?i:st|nd|rd|th)?,?\s+[0-9]{{4}}"  # Aug 14, 2026, Aug 14th, 2026
    rf"|[0-9]{{1,2}}(?i:er)?\s+{_MONTH},?\s+[0-9]{{4}}"  # 14 Aug 2026, 1er août 2026
)
_TIME = r"[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?(?:\s*(?i:[ap]\.?m\.?))?"  # 21:10, 9:10 PM, 9:10 p.m.
# Where chat exports put the timestamp: at the very start of the line, after an optional "["
# ("[2026-08-14 21:10] ...", "14/08/2026, 21:10 - ...", "Aug 14, 2026, 9:10 PM - ...",
# "14 août 2026 à 21:10 - ...", "21:10 Tenant: ..."), or after a name on a line that holds
# only the name, a date and a time (WeChat PC: "陈梅 2026-08-14 21:10", "Mei Aug 14, 2026 9:10 PM").
_LEADING_STAMP = re.compile(rf"^\ufeff?\[?(?:{_DATE}|{_TIME})(?![0-9])")
_NAME_STAMP = re.compile(rf"^\ufeff?([^\s:：][^:：]{{0,29}}?)\s+(?:{_DATE})[,\s]+{_TIME}$")
# A short name, an optional space and a colon: an ASCII one then a space or the end ("Tenant: ",
# not "https://"), or a full-width one, which Chinese text follows with no space ("房东：").
_SPEAKER = re.compile(r"^([^\s:：][^:：]{0,29}?)\s?(?::(?:\s|$)|：)")
# Letter and e-mail headers look like "Name:" (or "Date 2026-08-20 10:00") but are not
# speakers. Matched on the first word.
_HEADERS = frozenset({"to", "from", "date", "re", "subject", "cc", "bcc", "attn", "attention",
                      "dear", "sent", "address", "tel", "phone", "email", "e-mail"})


def read_txt(data: bytes) -> Pages:
    """The pages of read_txt_as, without the kind."""
    return read_txt_as(data)[1]


def read_txt_as(data: bytes, kind: TxtKind | None = None) -> tuple[TxtKind, Pages]:
    """One locator per non-empty line: "message 1", "message 2", ... for a chat export (one
    message per line, cited by number), "line 1", "line 2", ... for any other text, such as a
    letter. `kind` None decides by looks_like_chat; a caller that knows the kind (a tenant's
    typed answer is a message) passes it."""
    # The encodings sniff accepts; anything else as UTF-8, with control characters cleaned.
    decoded = sniff.decode_text(data)
    text = _clean(data.decode("utf-8-sig") if decoded is None else decoded)[:MAX_CHARS]
    # Newlines only: splitlines() would also break on form feeds and U+2028 inside a message.
    lines = [line.strip() for line in re.split(r"\r\n|\r|\n", text) if line.strip()]
    lines = lines[:MAX_MESSAGES]
    if kind is None:
        kind = "messages" if looks_like_chat(lines) else "letter"
    prefix = "message" if kind == "messages" else "line"
    return kind, [{"locator": f"{prefix} {n}", "text": line}
                  for n, line in enumerate(lines, start=1)]


def looks_like_chat(lines: Sequence[str]) -> bool:
    """Whether a TXT's non-empty lines are a chat or message export rather than a letter or
    other document (#125). A line is timestamped where chat exports put the timestamp: a date
    (2026-08-14, 14/08/2026, or an English or French month-name date with a 4-digit year such
    as Aug 14, 2026 or 14 août 2026) or a time (21:10, 9:10 PM) at the start of the line, after
    an optional "[" ("[2026-08-14 21:10] ...", "14/08/2026, 21:10 - ...",
    "Aug 14, 2026, 9:10 PM - ..."), or a line that is only a name, a date and a time (WeChat
    PC: "陈梅 2026-08-14 21:10", "Mei Aug 14, 2026 9:10 PM", the message on the next line). A
    date or time inside a sentence ("On 2026-08-14 I reported ...", "On Aug 14, 2026 I ...",
    "... at 9:30 pm") does not count. A line counts when it is timestamped or starts with a
    speaker prefix ("Tenant: ", "房东：", "Locataire : "; header words such as "To:", "Date:"
    or "Re:" are not speakers). The file is a chat when more than half of its lines count, or
    when at least MIN_STAMPED lines are timestamped: a WhatsApp export whose long messages run
    on over untimed lines, or a WeChat PC export, where only half the lines count.

    Deterministic and simple, so it can be wrong at the edges: a document of three or more
    lines that each begin with a date or time (a dated log or list of incidents, "2026-08-14:
    reported by phone") reads as a chat, and so does one whose lines are mostly "Name: text";
    a dated incident list whose lines start with "Aug 14, 2026: …" reads as a chat, just like
    the numeric one. A chat of one or two messages with long untimed continuation lines, or one
    whose export puts the timestamp elsewhere (at the end of a line, or a time alone after a
    name), reads as a letter. No lines: a chat (today's kind)."""
    if not lines:
        return True
    stamped = [_is_stamped(line) for line in lines]
    counted = sum(1 for line, stamp in zip(lines, stamped, strict=True)
                  if stamp or _is_speaker(line))
    return sum(stamped) >= MIN_STAMPED or counted * 2 > len(lines)


def _is_stamped(line: str) -> bool:
    if _LEADING_STAMP.match(line):
        return True
    named = _NAME_STAMP.match(line)
    return named is not None and not _is_header(named.group(1))


def _is_speaker(line: str) -> bool:
    match = _SPEAKER.match(line)
    return match is not None and not _is_header(match.group(1))


def _is_header(name: str) -> bool:
    return name.split()[0].casefold() in _HEADERS


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
