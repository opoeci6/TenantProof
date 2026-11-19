"""Turn uploaded files into evidence rows.

Callers have already checked the case (org) or the intake token. Flow per request:
1. check(): count, size and type of every file, before anything is stored (413 / 415).
2. Hash each file and find the ones the case already has (one short query).
3. With no database connection held: store each new original by sha256, read its metadata,
   store a stripped copy of photos, extract text. One file in memory at a time.
4. Under a short lock on the case row: insert the rows, give them E-NN labels, write their
   text as `evidence_page` rows, log each one, mark earlier runs stale.
A file that can't be read is kept with processing "failed"; it never fails the upload."""

import hashlib
import io
import logging
import re
import uuid
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, BinaryIO, Literal

from PIL import Image
from sqlmodel import Session, col, func, select

from app.config import get_settings
from app.models import CaseRow, EvidencePageRow, EvidenceRow, RunRow
from app.services import activity, review, storage
from app.services.evidence import exif, fingerprint, ocr, sniff, strip, text

log = logging.getLogger(__name__)

Uploader = Literal["tenant", "caseworker"]
MAX_FILES = 20
CHUNK = 1024 * 1024

_KIND = {sniff.JPEG: "photo", sniff.PNG: "photo", sniff.HEIC: "photo", sniff.PDF: "other",
         sniff.TEXT: "messages"}
_FORMAT: dict[str, dict[str, Any]] = {
    sniff.JPEG: {"accepted": True, "action": "none", "note": "JPEG is an accepted format."},
    sniff.PNG: {"accepted": True, "action": "none", "note": "PNG is an accepted format."},
    sniff.PDF: {"accepted": True, "action": "none", "note": "PDF is an accepted format."},
    sniff.HEIC: {"accepted": True, "action": "converted",
                 "note": "HEIC isn't an accepted format. The package files a JPG copy instead."},
    sniff.TEXT: {"accepted": True, "action": "will_export_pdf",
                 "note": "Text messages must be submitted digitally or printed. "
                         "The package adds a PDF copy."},
}
_NAME_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_NAME = 120
# Text read by OCR is cited under its own locator, so the caseworker sees it was machine-read.
PHOTO_OCR_LOCATOR = "text in the photo (OCR)"
OCR_SKIPPED = ("The text in this file was not read: the server's time for reading images in "
               "this upload ran out. The file is kept as it is.")


class UploadRejected(Exception):
    """The whole request is refused and nothing is stored. `status_code` is 413 or 415."""

    def __init__(self, status_code: int, reasons: list[str]) -> None:
        super().__init__(reasons)
        self.status_code = status_code
        self.reasons = reasons


@dataclass(frozen=True)
class Upload:
    filename: str  # display only; never used as a path
    mime: str
    size: int
    source: BinaryIO  # the spooled upload, rewound


@dataclass
class _Processed:
    metadata: dict[str, Any] = field(default_factory=exif.NO_META.as_dict)
    stripped_sha256: str | None = None
    pages: text.Pages = field(default_factory=list)  # becomes evidence_page rows
    processing: str = "done"
    error: str | None = None
    # #127: a perceptual hash of the image, kept in the metadata JSON; it only ever feeds the
    # "possible duplicate" note and is never used to merge or drop a file.
    image_hash: str | None = None


@dataclass(frozen=True)
class _Ready:
    upload: Upload
    sha256: str
    done: _Processed


def display_name(raw: str | None) -> str:
    """Last path segment, no control characters, capped. Shown to people, never opened."""
    name = re.split(r"[\\/]", raw or "")[-1]
    name = _NAME_CONTROL.sub("", name).strip()
    return name[-MAX_NAME:] or "file"


def check(files: Sequence[tuple[str | None, BinaryIO]], max_bytes: int) -> list[Upload]:
    """Validate every file before storing any. Raises UploadRejected (413 before 415)."""
    if len(files) > MAX_FILES:
        raise UploadRejected(413, [f"Send at most {MAX_FILES} files at a time"])
    sizes = [_size(source) for _, source in files]
    too_big = [f"File {n} is larger than {max_bytes // (1024 * 1024)} MB"
               for n, size in enumerate(sizes, 1) if size > max_bytes]
    if too_big:
        raise UploadRejected(413, too_big)
    uploads, wrong_type = [], []
    for n, ((name, source), size) in enumerate(zip(files, sizes, strict=True), 1):
        mime = sniff.sniff_file(source, max_bytes)
        if mime is None:
            wrong_type.append(_wrong_type(n, source, max_bytes))
        else:
            uploads.append(Upload(filename=display_name(name), mime=mime, size=size,
                                  source=source))
    if wrong_type:
        raise UploadRejected(415, wrong_type)
    return uploads


def _wrong_type(n: int, source: BinaryIO, max_bytes: int) -> str:
    """A file we can't read as text is still a TXT, so don't answer "not an accepted type" and
    send the tenant hunting for a format: say what fixes it. Re-saving as UTF-8 does."""
    if sniff.looks_like_text_file(source, max_bytes):
        return f"File {n} is text in an encoding this app can't read. Re-save it as UTF-8"
    return f"File {n} is not an accepted type ({sniff.ALLOWED_LABEL})"


def ingest(
    session: Session, case: CaseRow, uploads: list[Upload], uploaded_by: Uploader,
    actor_name: str,
) -> list[EvidenceRow]:
    """Add the files to the case and return one row per upload, in order. A file the case
    already has comes back as its existing row. Caller commits."""
    case_id = case.id
    digests = [_sha256(u.source) for u in uploads]
    known = set(session.exec(
        select(EvidenceRow.sha256).where(EvidenceRow.case_id == case_id,
                                         col(EvidenceRow.sha256).in_(digests))
    ).all())
    session.rollback()  # nothing written yet: give the connection back while files are processed
    settings = get_settings()
    budget = ocr.Budget(settings.ocr_request_seconds) if settings.ocr_enabled else None
    ready: dict[str, _Ready] = {}
    for upload, digest in zip(uploads, digests, strict=True):
        if digest not in known and digest not in ready:
            ready[digest] = _prepare(upload, digest, budget)
    return _insert(session, case_id, digests, list(ready.values()), uploaded_by, actor_name)


def model_safe_blob(row: EvidenceRow) -> str | None:
    """The only stored bytes of a photo that may be sent to the model: the stripped copy.
    Never `row.sha256` (the original keeps its EXIF and GPS). Documents go to the model as
    their `evidence_page` rows, never as files."""
    return row.stripped_sha256


def _size(source: BinaryIO) -> int:
    source.seek(0, io.SEEK_END)
    size = source.tell()
    source.seek(0)
    return size


def _sha256(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    source.seek(0)
    while chunk := source.read(CHUNK):
        digest.update(chunk)
    source.seek(0)
    return digest.hexdigest()


def _prepare(upload: Upload, digest: str, budget: ocr.Budget | None = None) -> _Ready:
    data = upload.source.read()
    stored = storage.put(data)
    assert stored.key == digest
    return _Ready(upload=upload, sha256=digest, done=_process(data, upload.mime, budget))


def _process(data: bytes, mime: str, budget: ocr.Budget | None = None) -> _Processed:
    """`budget` None: no OCR (OCR_ENABLED=false, or a caller that only wants the text layer)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            return _process_unchecked(data, mime, budget)
    except text.PdfTimeout:
        log.warning("evidence processing timed out for %s", mime)
        return _Processed(processing="failed", error="Reading the text took too long.")
    except Exception as exc:  # any decoder error: keep the original, flag it, never fail
        log.warning("evidence processing failed for %s: %s", mime, type(exc).__name__)
        return _Processed(processing="failed", error="This file could not be read.")


def _process_unchecked(data: bytes, mime: str, budget: ocr.Budget | None) -> _Processed:
    if mime == sniff.PDF:
        # A child process with a hard time limit; it renders scanned pages only for OCR.
        pdf = text.read_pdf_isolated(data, scans=budget is not None)
        pages, skipped = _ocr_scans(pdf, budget)
        partial = None if pdf.complete else "Only part of the text could be extracted."
        scan = next(iter(pdf.scans.values()), None)
        return _Processed(
            metadata={**exif.NO_META.as_dict(), "software": pdf.software},
            pages=pages,
            error=partial or (OCR_SKIPPED if skipped else None),
            image_hash=fingerprint.dhash_bytes(scan) if scan else None,
        )
    if mime == sniff.TEXT:
        return _Processed(pages=text.read_txt(data))
    with Image.open(io.BytesIO(data)) as img:
        meta = exif.read_meta(img).as_dict()  # before strip_image changes the image
        clean, _ = strip.strip_image(img, mime)  # also turns it upright and checks its size
        pages = [exif.photo_page(meta["captured_at"])]  # so the agent can cite the capture date
        found, skipped = _ocr(img, budget) if budget is not None else ("", False)
        if found:
            pages.append({"locator": PHOTO_OCR_LOCATOR, "text": found})
    return _Processed(
        metadata=meta, stripped_sha256=storage.put(clean).key, pages=pages,
        error=OCR_SKIPPED if skipped else None,
        image_hash=fingerprint.dhash_bytes(clean),
    )


def _ocr(img: Image.Image, budget: ocr.Budget) -> tuple[str, bool]:
    """OCR that can't fail the upload. Returns (text, skipped): skipped when this request's OCR
    time is used up, before or while waiting for the engine. On any other error the file is
    kept without its text."""
    if not budget.left():
        return "", True
    try:
        return budget.read(img), False
    except ocr.Busy:
        return "", True
    except Exception as exc:
        log.warning("OCR failed: %s", type(exc).__name__)  # type only
        return "", False


def _ocr_png(png: bytes, budget: ocr.Budget) -> tuple[str, bool]:
    """A scanned page's grey PNG, made by our own PDF child process, read by OCR."""
    try:
        with Image.open(io.BytesIO(png)) as img:
            return _ocr(img, budget)
    except Exception as exc:
        log.warning("OCR of a scanned page failed: %s", type(exc).__name__)  # type only
        return "", False


def _ocr_scans(pdf: text.PdfText, budget: ocr.Budget | None) -> tuple[text.Pages, bool]:
    """The PDF's pages, with each scanned page's text read by OCR under "page N (OCR)". Returns
    the pages and whether the budget ran out before every scan was read."""
    if budget is None or not pdf.scans:
        return pdf.pages, False
    pages, skipped = [], False
    for page in pdf.pages:
        png = pdf.scans.get(page["locator"])
        if png is not None:
            found, missed = _ocr_png(png, budget)
            skipped = skipped or missed
            if found:
                page = {"locator": f"{page['locator']} (OCR)", "text": found}
        pages.append(page)
    return pages, skipped


def _insert(
    session: Session, case_id: str, digests: list[str], ready: list[_Ready],
    uploaded_by: Uploader, actor_name: str,
) -> list[EvidenceRow]:
    # FOR NO KEY UPDATE: serialises uploads on this case (labels) without blocking the
    # foreign-key checks other writes on the case make.
    case = session.exec(
        select(CaseRow).where(CaseRow.id == case_id).with_for_update(key_share=True)
    ).one()
    last = session.exec(
        select(func.max(EvidenceRow.position)).where(EvidenceRow.case_id == case_id)
    ).one()
    position = -1 if last is None else last
    now = datetime.now(UTC)
    added = 0
    # #127: labels and fingerprints of what the case already has, so a photo and a PDF made from
    # it can be called out as the same picture instead of looking like two independent files.
    known = [
        (row.label, (row.metadata_ or {}).get("image_hash"))
        for row in session.exec(select(EvidenceRow).where(EvidenceRow.case_id == case_id)).all()
    ]
    for item in ready:
        if _find(session, case_id, item.sha256) is not None:
            continue  # another request added the same file meanwhile
        position += 1
        row = _new_row(case_id, position, item, uploaded_by, now)
        session.add(row)
        session.flush()
        for n, page in enumerate(item.done.pages):
            session.add(EvidencePageRow(evidence_id=row.id, locator=page["locator"],
                                        case_id=case_id, position=n, text=page["text"]))
        duplicate = next(
            (label for label, image in known
             if fingerprint.looks_the_same(item.done.image_hash, image)),
            None,
        )
        if duplicate:
            row.format_check = {
                **row.format_check,
                "note": f"{row.format_check['note']} Possible duplicate of {duplicate}: "
                        "the image looks the same.",
            }
        known.append((row.label, item.done.image_hash))
        activity.log(session, case_id, uploaded_by, actor_name,
                     f"Uploaded {row.label} ({row.filename})")
        added += 1
    if added:
        _after_new_evidence(session, case, uploaded_by, now)
    rows = [_find(session, case_id, d) for d in digests]
    return [r for r in rows if r is not None]


def _find(session: Session, case_id: str, digest: str) -> EvidenceRow | None:
    return session.exec(
        select(EvidenceRow).where(EvidenceRow.case_id == case_id,
                                  col(EvidenceRow.sha256) == digest)
    ).first()


def _new_row(
    case_id: str, position: int, item: _Ready, uploaded_by: Uploader, now: datetime
) -> EvidenceRow:
    upload, done = item.upload, item.done
    return EvidenceRow(
        id=f"ev_{uuid.uuid4().hex[:16]}", case_id=case_id, position=position,
        label=f"E-{position + 1:02d}", filename=upload.filename, kind=_KIND[upload.mime],
        mime=upload.mime, size_bytes=upload.size, sha256=item.sha256, received_at=now,
        uploaded_by=uploaded_by, format_check=_format_check(upload.mime, done.processing),
        # #127: the fingerprint rides in the metadata JSON; EvidenceMetadata ignores it, the
        # duplicate check reads it back.
        metadata_={**done.metadata, **({"image_hash": done.image_hash} if done.image_hash else {})},
        metadata_stripped_for_ai=done.processing == "done",
        processing=done.processing, error=done.error, stripped_sha256=done.stripped_sha256,
    )


_NO_COPY = {
    sniff.HEIC: "HEIC isn't an accepted format, and this file couldn't be read, so the package "
                "can't add a JPG copy. Ask for the photo as a JPG.",
    sniff.TEXT: "Text messages must be submitted digitally or printed. This file couldn't be "
                "read, so the package can't add a PDF copy.",
}


def _format_check(mime: str, processing: str) -> dict[str, Any]:
    """The package makes no copy of a file that failed (package._rendition), so don't promise
    one: HEIC and TXT then aren't in a format the tribunal takes."""
    if processing == "failed" and mime in _NO_COPY:
        return {"accepted": False, "action": "none", "note": _NO_COPY[mime]}
    return dict(_FORMAT[mime])


def _after_new_evidence(
    session: Session, case: CaseRow, uploaded_by: Uploader, now: datetime
) -> None:
    for run in session.exec(select(RunRow).where(RunRow.case_id == case.id)).all():
        if run.started_at < now and not run.stale:
            run.stale = True  # the UI tells the caseworker to run the analysis again
            session.add(run)
    if uploaded_by == "tenant":
        case.intake_status = "files_received"
        case.intake_last_upload_at = now
        if case.tenant_attested_genuine:
            # The confirmation covered the files sent before it. The intake page sends the
            # files and then the confirmation again; until then the package can't be built.
            case.tenant_attested_genuine = False
            case.tenant_attested_at = None
            activity.log(session, case.id, "tenant", case.tenant_name,
                         "New files arrived after the confirmation, so it must be given again")
            review.sync_attestation_gap(session, case)
    case.updated_at = now
    session.add(case)
