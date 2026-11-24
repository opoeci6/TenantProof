"""Filing package: two PDFs, manifest.json and the original evidence files in a zip, stored
content-addressed. Evidence entries are named from the exhibit label (`evidence/E-01.pdf`),
never from the uploaded filename; a row whose original was never stored (seed data) is listed
in the manifest with `in_zip: null`. The Program doesn't take HEIC (Evidence Policy #42), so a
HEIC is filed only as a JPG made from it: no `.heic` file is ever in the zip, and a HEIC that
cannot be converted is left out and said so in the manifest. Text messages are filed as PDF, so the
zip also carries a PDF made from each TXT (`rendition` in the manifest). Every file has its own
sha256; other originals stay in the zip.

Every build is a new version (never overwritten). The zip's sha256 goes on the row and in the
activity entry. `caseworker_note` is never read here. Text uses Noto Sans with Noto Sans SC as
fallback so Chinese renders (core PDF fonts cannot encode CJK)."""

import hashlib
import io
import json
import logging
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from fpdf import FPDF
from fpdf.enums import WrapMode
from PIL import Image
from sqlmodel import Session, col, select

from app.models import CaseRow, EvidenceRow, FormFieldRow, GapRow, PackageRow, UserRow
from app.schemas import Attestations, Package, PackageFile
from app.services import activity, storage
from app.services.evidence import gps, sniff  # also registers the HEIC opener
from app.services.gating import MissingField, OpenGap, package_blockers
from app.services.mappers import (
    OPEN_GAP,
    build_form_draft,
    has_completed_run,
    iso_req,
    latest_run_steps,
    reviewed_event_count,
)

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
REMINDERS = [
    "Evidence must be received at least 5 days before the hearing "
    "(Evidence Policy #42, Regulations s. 25A).",
    "Evidence sent by registered mail, express post or courier counts as received on the 3rd "
    "day after it is sent. E-mail counts as received the same day if sent before 4 p.m., and "
    "the next day if sent at or after 4 p.m.",
    "The Program does not accept zip files. This zip is for your records: upload the files "
    "inside it one by one.",
    "This package is prepared information, not legal advice. The tenant files the form themselves.",
    "Nothing has been submitted anywhere. Check every field against the official form.",
]
EVIDENCE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic",
                "application/pdf": ".pdf", "text/plain": ".txt"}
NOT_STORED = "Original file not in storage (seeded demo rows have none)"
HEIC_NOT_FILED = "Not filed: HEIC is not an accepted format and no JPG copy could be made"
GPS_NOT_FILED = "Not filed: the location data in this image could not be removed"
UNREADABLE_NOT_FILED = (
    "Not filed: this image could not be read, so it could not be checked for location data"
)
LOCATION_REMOVED = "location data removed"
log = logging.getLogger(__name__)
ATTESTATION_LABELS = {
    "reviewed_fields": "Confirm you reviewed every field and the summary",
    "remedy_confirmed_with_tenant": "Confirm the tenant has said what they are asking for",
    "explained_not_legal_advice": "Confirm you told the tenant this is information, not a decision",
}


@dataclass(frozen=True)
class BuiltZip:
    data: bytes
    sha256: str
    files: list[dict[str, str]]


def attestation_reasons(att: Attestations) -> list[str]:
    return [label for key, label in ATTESTATION_LABELS.items() if not getattr(att, key)]


NOT_ANALYSED = "No analysis has been completed for this case yet. Run the analysis first."
NOTHING_REVIEWED = "No timeline events have been accepted yet. Review the timeline first."
# #119. The same words as `analysisWarning` in web/index.html.
STOPPED_TIMELINE = (
    "The analysis stopped early: the timeline may be missing events. "
    "Run the analysis again before relying on this."
)
STOPPED_OTHER = (
    "The analysis stopped early: some results may be missing. "
    "Run the analysis again before relying on this."
)


def incomplete_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in steps if s.get("status") == "incomplete"]


def analysis_warning(stopped: list[dict[str, Any]]) -> list[str]:
    """What the brief and the package say about steps that stopped early; empty when none did."""
    if not stopped:
        return []
    lead = STOPPED_TIMELINE if any(s["key"] == "timeline" for s in stopped) else STOPPED_OTHER
    return [lead, f"Stopped early: {', '.join(s['label'] for s in stopped)}"]


def analysis_reasons(session: Session, case: CaseRow) -> list[str]:
    """A package needs a finished run and at least one accepted or edited event."""
    reasons = []
    if not has_completed_run(session, case.id):
        reasons.append(NOT_ANALYSED)
    if reviewed_event_count(session, case.id) == 0:
        reasons.append(NOTHING_REVIEWED)
    return reasons


def state_reasons(session: Session, case: CaseRow) -> list[str]:
    fields = session.exec(select(FormFieldRow).where(FormFieldRow.case_id == case.id)).all()
    gaps = session.exec(select(GapRow).where(GapRow.case_id == case.id)).all()
    reasons = package_blockers(
        [OpenGap(g.title, (g.input or {}).get("field_key"))
         for g in gaps if g.severity == "blocking" and g.status in OPEN_GAP],
        [MissingField(f.key, f.label) for f in fields if f.required and not f.value],
        case.tenant_attested_genuine,
    )
    if not fields:
        reasons.insert(0, "No form draft has been prepared for this case yet")
    return [*analysis_reasons(session, case), *reasons]


# Hindi cannot be drawn correctly yet: the bundled fonts have the Devanagari glyphs, but fpdf2 only
# joins consonants and places vowel signs with text shaping (`uharfbuzz`, not a dependency).
# Unshaped Devanagari reads wrongly ("हिन्दी" comes out "हन्िदी"), which is worse than leaving it
# out, so for a Hindi case every PDF shows a marker where Devanagari text would be, says so, and
# the original files carry the text. Issue #68 keeps the real fix.
HINDI_NOTE = "PDFs are in English; Hindi PDF rendering is not supported yet."
HINDI_MARK = "[Hindi text, see the original file]"
_DEVANAGARI = re.compile("[\u0900-\u097f]+")


class TenantPdf(FPDF):
    hindi_unsupported = False


def has_devanagari(text: str) -> bool:
    return _DEVANAGARI.search(text) is not None


def pdf_text(pdf: FPDF, text: str) -> str:
    """The text as it goes in this PDF: Devanagari runs become a marker in a Hindi case's PDFs."""
    if getattr(pdf, "hindi_unsupported", False):
        return _DEVANAGARI.sub(HINDI_MARK, text)
    return text


def new_pdf(language: str = "en") -> FPDF:
    pdf = TenantPdf(format="Letter")
    pdf.hindi_unsupported = language == "hi"
    pdf.add_font("NotoSans", "", FONT_DIR / "NotoSans-Regular.ttf")
    pdf.add_font("NotoSans", "B", FONT_DIR / "NotoSans-Bold.ttf")
    pdf.add_font("NotoSansSC", "", FONT_DIR / "NotoSansSC-Regular.ttf")
    pdf.set_fallback_fonts(["NotoSansSC"], exact_match=False)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("NotoSans", size=10)
    return pdf


def _write(pdf: FPDF, text: str, *, bold: bool = False, size: int = 10, gap: float = 1.5) -> None:
    pdf.set_font("NotoSans", "B" if bold else "", size)
    # CHAR wrapping: Chinese text has no spaces to break on.
    pdf.multi_cell(
        0, size * 0.5 + 1.5, pdf_text(pdf, text),
        new_x="LMARGIN", new_y="NEXT", wrapmode=WrapMode.CHAR,
    )
    pdf.ln(gap)


def application_pdf(session: Session, case: CaseRow, prepared_by: str, built_at: datetime) -> bytes:
    draft = build_form_draft(session, case)
    pdf = new_pdf(case.tenant_language)
    _write(pdf, draft.form_name or "Application draft", bold=True, size=16, gap=3)
    _write(pdf, draft.disclaimer, size=9, gap=4)
    _write(pdf, f"Case: {case.title} ({case.unit})")
    _write(pdf, f"Prepared by {prepared_by} on {built_at:%Y-%m-%d %H:%M} UTC", gap=4)
    for field in draft.fields:
        _write(pdf, field.label, bold=True, gap=0)
        _write(pdf, field.value or "(not filled in)")
    _write(pdf, "Summary of events", bold=True, size=12, gap=2)
    _write(pdf, draft.narrative.text or "(no accepted events)")
    return bytes(pdf.output())


def evidence_zip_name(ev: EvidenceRow) -> str | None:
    """Zip path for the original file, or None when its bytes were never stored."""
    if not storage.exists(ev.sha256):
        return None
    label = "".join(c for c in ev.label if c.isalnum() or c == "-") or ev.id
    return f"evidence/{label}{EVIDENCE_EXT.get(ev.mime, '.bin')}"


@dataclass(frozen=True)
class EvidenceFile:
    """One evidence entry in the zip: an original (streamed from storage) or a rendition."""

    label: str
    name: str
    sha256: str
    note: str
    data: bytes | None = None  # None: the original, read from storage by sha256
    location_removed: bool = False  # a copy made with its GPS data taken out


def evidence_files(ev: EvidenceRow, language: str = "en") -> list[EvidenceFile]:
    """What goes in the zip for one piece of evidence: the original, plus a PDF made from a TXT.
    A HEIC is never filed: the Program doesn't accept it, so only the JPG made from it goes in
    (nothing at all if it can't be converted). A JPG or PNG with GPS data is filed as a copy
    without it (the stored original is untouched), and left out if that can't be done. Empty
    when the original was never stored."""
    name = evidence_zip_name(ev)
    if name is None:
        return []
    made = _rendition(ev, name.rsplit(".", 1)[0], language)
    if ev.mime == sniff.HEIC:
        return [made] if made is not None else []
    original = _original(ev, name)
    if original is None:
        return []
    files = [original]
    if made is not None:
        files.append(made)
    return files


def _original(ev: EvidenceRow, name: str) -> EvidenceFile | None:
    """The original as it goes in the zip. A JPG or PNG with GPS data becomes a copy without it:
    same pixels, same capture time and orientation, only the location taken out. None when that
    could not be done, because an image must not be filed with its location in it."""
    plain = EvidenceFile(ev.label, name, ev.sha256, ev.filename)
    if ev.mime not in (sniff.JPEG, sniff.PNG):
        return plain
    try:
        data, removed = gps.without_gps(storage.read(ev.sha256), ev.mime)
    except Exception as exc:
        log.warning("could not remove location data from %s: %s", ev.label, type(exc).__name__)
        return None
    if not removed:
        return plain
    return EvidenceFile(ev.label, name, _sha(data), f"{ev.filename}; {LOCATION_REMOVED}",
                        data, location_removed=True)


def _rendition(ev: EvidenceRow, stem: str, language: str = "en") -> EvidenceFile | None:
    if ev.processing == "failed":
        return None  # couldn't be read at ingest (corrupt, or over the pixel limit): don't decode
    try:
        if ev.mime == sniff.HEIC:
            raw = storage.read(ev.sha256)
            data = heic_to_jpeg(raw)
            removed = gps.exif_has_gps(raw)
            note = (f"JPG made from the HEIC original ({ev.filename}); "
                    "the Program does not accept HEIC")
            if removed:
                note += f"; {LOCATION_REMOVED}"
            return EvidenceFile(ev.label, f"{stem}.jpg", _sha(data), note, data,
                                location_removed=removed)
        if ev.mime == sniff.TEXT:
            decoded = sniff.decode_text(storage.read(ev.sha256))
            if decoded is None:  # not reachable for a file the type check accepted
                return None
            if language == "hi" and has_devanagari(decoded):
                return None  # a PDF of it would be mostly markers; the original is filed as is
            data = text_to_pdf(ev, decoded, language)
            return EvidenceFile(ev.label, f"{stem}.pdf", _sha(data),
                                f"PDF made from the text file ({ev.filename})", data)
    except Exception as exc:  # the original is still filed; a missing copy must not block
        log.warning("could not make a filing copy of %s: %s", ev.label, type(exc).__name__)
    return None


def heic_to_jpeg(data: bytes) -> bytes:
    """Same pixels and the same EXIF as the original, minus the GPS data (this copy goes in the
    package). Not the stripped copy: that one is only for the model."""
    with Image.open(io.BytesIO(data)) as img:
        exif = img.getexif()
        if gps.GPS_IFD in exif:
            del exif[gps.GPS_IFD]
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=92, exif=exif.tobytes())
    return out.getvalue()


def text_to_pdf(ev: EvidenceRow, text: str, language: str = "en") -> bytes:
    pdf = new_pdf(language)
    _write(pdf, f"{ev.label}  {ev.filename}", bold=True, size=12, gap=1)
    _write(pdf, f"Text file, sha256 {ev.sha256}", size=8, gap=4)
    _write(pdf, text or "(empty)", size=9)
    return bytes(pdf.output())


def manifest_pdf(
    evidence: list[EvidenceRow], case: CaseRow, ev_files: dict[str, list[EvidenceFile]],
    warning: list[str],
) -> bytes:
    pdf = new_pdf(case.tenant_language)
    _write(pdf, "Evidence manifest", bold=True, size=16, gap=3)
    _write(pdf, f"Case: {case.title}", gap=2 if case.tenant_language != "hi" else 1)
    # #127: the receipt time is the time the whole upload arrived, so four files sent together
    # share it. Say so rather than leave it looking like a mistake.
    _write(pdf, "Files uploaded together share one receipt time.", size=8, gap=4)
    if case.tenant_language == "hi":
        _write(pdf, HINDI_NOTE, size=9, gap=4)
    for line in warning:
        _write(pdf, line, bold=True)
    if not evidence:
        _write(pdf, "No evidence files are listed.")
    for ev in evidence:
        _write(pdf, f"{ev.label}  {ev.filename}", bold=True, gap=0)
        # Like mappers.iso(): a naive time is already UTC, so it must not be read as local time.
        at = ev.received_at if ev.received_at.tzinfo else ev.received_at.replace(tzinfo=UTC)
        received = at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        _write(pdf, f"Received {received} from {ev.uploaded_by}", gap=0)
        filed = ev_files[ev.id]
        removed = any(item.location_removed for item in filed)
        gps = bool((ev.metadata_ or {}).get("gps_present"))
        location = ("removed before filing" if removed else
                    "none in the file" if not gps else "could not be removed; see below")
        _write(pdf, f"Location data: {location}", size=8, gap=0)
        heic = ev.mime == sniff.HEIC
        # A HEIC is received as HEIC but filed only as its JPG: "Filed as", not "Also as".
        copies = filed if heic else filed[1:]
        _write(pdf, f"sha256 {ev.sha256}", size=8, gap=0 if copies or not filed else 3)
        for copy in copies:
            lead = "Filed as" if heic else "Also as"
            _write(pdf, f"{lead} {copy.name}, sha256 {copy.sha256}", size=8, gap=3)
        for item in filed:
            if item.location_removed:
                _write(pdf, f"{item.name}, sha256 {item.sha256}: {LOCATION_REMOVED}",
                       size=8, gap=3)
        if not filed:
            _write(pdf, _unfiled_reason(ev), size=8, gap=3)
    return bytes(pdf.output())


def _unfiled_reason(ev: EvidenceRow) -> str:
    if not storage.exists(ev.sha256):
        return NOT_STORED
    if ev.mime == sniff.HEIC:
        return HEIC_NOT_FILED
    # Corrupt: the GPS step can't open it either, so "couldn't be removed" would mislead.
    return UNREADABLE_NOT_FILED if ev.processing == "failed" else GPS_NOT_FILED


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_zip(
    session: Session, case: CaseRow, prepared_by: str, att: Attestations, version: int,
    built_at: datetime,
) -> BuiltZip:
    evidence = list(session.exec(
        select(EvidenceRow)
        .where(EvidenceRow.case_id == case.id)
        .order_by(col(EvidenceRow.position))
    ).all())
    app_pdf = application_pdf(session, case, prepared_by, built_at)
    ev_files = {e.id: evidence_files(e, case.tenant_language) for e in evidence}
    stopped = incomplete_steps(latest_run_steps(session, case.id))
    warning = analysis_warning(stopped)
    man_pdf = manifest_pdf(evidence, case, ev_files, warning)
    manifest: dict[str, Any] = {
        "case_id": case.id, "version": version, "built_at": iso_req(built_at),
        "prepared_by": prepared_by,
        "received_note": "Files uploaded together share one receipt time.",
        "tenant_attested_genuine_at": iso_req(case.tenant_attested_at)
        if case.tenant_attested_at else None,
        "caseworker_attestations": att.model_dump(),
        "analysis_incomplete": bool(stopped),
        "incomplete_steps": [s["label"] for s in stopped],
        "documents": {
            "application_draft.pdf": _sha(app_pdf),
            "evidence_manifest.pdf": _sha(man_pdf),
        },
        "evidence": [
            {"label": e.label, "filename": e.filename, "sha256": e.sha256,
             "received_at": iso_req(e.received_at), "uploaded_by": e.uploaded_by,
             "in_zip": ev_files[e.id][0].name if ev_files[e.id] else None,
             "location_data_removed": any(f.location_removed for f in ev_files[e.id]),
             # #127: also say whether the file had location data at all; the boolean above
             # (kept for existing readers) was false and ambiguous when there was none.
             "location": {"present": bool((e.metadata_ or {}).get("gps_present")),
                          "removed": any(f.location_removed for f in ev_files[e.id])},
             "rendition": {"path": ev_files[e.id][1].name, "sha256": ev_files[e.id][1].sha256}
             if len(ev_files[e.id]) > 1 and e.mime != sniff.HEIC else None}
            for e in evidence
        ],
    }
    notes = [*warning, *([HINDI_NOTE] if case.tenant_language == "hi" else [])]
    if notes:
        manifest["notes"] = notes
    man_json = json.dumps(manifest, indent=2, ensure_ascii=False).encode()
    parts = [
        ("application_draft.pdf", app_pdf, "Application draft", "From the reviewed timeline"),
        ("evidence_manifest.pdf", man_pdf, "Evidence manifest", "Each file with its hash"),
        ("manifest.json", man_json, "Machine-readable manifest", "Hashes, times, uploaders"),
    ]
    stamp = built_at.timetuple()[:6]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data, _, _ in parts:
            archive.writestr(zipfile.ZipInfo(name, date_time=stamp), data)
        for ev in evidence:
            for item in ev_files[ev.id]:
                info = zipfile.ZipInfo(item.name, date_time=stamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                with archive.open(info, "w") as out:
                    if item.data is not None:
                        out.write(item.data)
                    else:
                        for chunk in storage.open_chunks(item.sha256):
                            out.write(chunk)
    files = [{"label": lbl, "filename": n, "sha256": _sha(d), "note": note}
             for n, d, lbl, note in parts]
    # Only what is actually in the zip; unstored seed rows are in the manifests only.
    files += [{"label": f.label, "filename": f.name, "sha256": f.sha256, "note": f.note}
              for e in evidence for f in ev_files[e.id]]
    data = buffer.getvalue()
    return BuiltZip(data=data, sha256=_sha(data), files=files)


def build_package(
    session: Session, case: CaseRow, user: UserRow, att: Attestations
) -> PackageRow:
    """Caller has already checked attestations (422) and state (409). Caller commits."""
    # Lock the case row so two concurrent builds cannot take the same version number.
    session.exec(select(CaseRow).where(CaseRow.id == case.id).with_for_update()).one()
    latest = session.exec(
        select(PackageRow.version).where(PackageRow.case_id == case.id)
        .order_by(col(PackageRow.version).desc())
    ).first()
    version = (latest or 0) + 1
    built_at = datetime.now(UTC)
    built = build_zip(session, case, user.name, att, version, built_at)
    blob = storage.put(built.data)
    row = PackageRow(
        id=uuid.uuid4().hex, case_id=case.id, version=version, sha256=built.sha256,
        storage_key=blob.key, size_bytes=blob.size_bytes, prepared_by=user.name,
        built_at=built_at, attestations=att.model_dump(), files=built.files,
    )
    session.add(row)
    activity.log(session, case.id, "caseworker", user.name,
                 f"Built filing package v{version} (sha256 {built.sha256})")
    return row


def to_package(row: PackageRow) -> Package:
    return Package(
        version=row.version, sha256=row.sha256, built_at=iso_req(row.built_at),
        prepared_by=row.prepared_by, files=[PackageFile(**f) for f in row.files],
        reminders=REMINDERS,
    )


def get_package(session: Session, case: CaseRow, version: int | None) -> PackageRow:
    query = select(PackageRow).where(PackageRow.case_id == case.id)
    query = (
        query.where(PackageRow.version == version) if version is not None
        else query.order_by(col(PackageRow.version).desc())
    )
    row = session.exec(query).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Package not found")
    return row
