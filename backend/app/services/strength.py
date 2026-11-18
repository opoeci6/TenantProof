"""Event strength and flags (BUILD_PLAN section 6). Pure: no I/O, no LLM.

Strength is computed from the sources and the date check, never chosen by the model."""

from dataclasses import dataclass

from app.schemas import EventDate, EventFlag
from app.services.dates import date_inferred

# Matched case-insensitively as substrings of the EXIF Software field. A PDF producer such as
# "Adobe Acrobat" is not an image editor.
EDITOR_SOFTWARE = (
    "photoshop", "lightroom", "gimp", "snapseed", "pixelmator", "affinity photo", "picsart",
    "facetune", "canva", "paint.net", "pixlr", "capture one",
)  # fmt: skip


@dataclass(frozen=True)
class SourceFacts:
    """What strength needs to know about one cited evidence item."""

    evidence_id: str
    kind: str
    uploaded_by: str
    captured_at: str | None
    software: str | None

    @property
    def edited(self) -> bool:
        software = (self.software or "").lower()
        return any(name in software for name in EDITOR_SOFTWARE)

    @property
    def no_metadata(self) -> bool:
        return self.kind == "photo" and not self.captured_at


@dataclass(frozen=True)
class Strength:
    strength: str  # corroborated | single_source | needs_support
    reasons: list[str]


def event_flags(
    sources: list[SourceFacts], chosen: EventDate, *, mismatch: bool
) -> list[EventFlag]:
    flags: list[EventFlag] = []
    if any(s.no_metadata for s in sources):
        flags.append(EventFlag(type="no_metadata", message="A photo has no capture time."))
    if any(s.edited for s in sources):
        flags.append(
            EventFlag(type="edited_software", message="A file was last saved by an image editor.")
        )
    if date_inferred(chosen):
        flags.append(EventFlag(type="date_inferred", message="The date comes from a file name."))
    if mismatch:
        flags.append(EventFlag(type="date_mismatch", message="The sources give different dates."))
    return flags


def _independent(sources: list[SourceFacts]) -> bool:
    """At least two different evidence items that differ in kind or uploader."""
    for i, a in enumerate(sources):
        for b in sources[i + 1 :]:
            if a.evidence_id != b.evidence_id and (
                a.kind != b.kind or a.uploaded_by != b.uploaded_by
            ):
                return True
    return False


def compute_strength(sources: list[SourceFacts], *, mismatch: bool) -> Strength:
    """`sources` are the valid (verified) sources only."""
    if not sources:
        return Strength("needs_support", ["No verified source."])
    if all(s.edited or mismatch for s in sources):
        why = "The sources give different dates." if mismatch else "Every file was edited."
        return Strength("needs_support", [why])
    reasons: list[str] = []
    if len(sources) == 1:
        return Strength("single_source", ["One verified source."])
    reasons.append(f"{len(sources)} verified sources.")
    if not _independent(sources):
        reasons.append("The sources are not independent (same kind and uploader, or same file).")
        return Strength("single_source", reasons)
    if any(s.edited for s in sources):
        reasons.append("A file was last saved by an image editor.")
        return Strength("single_source", reasons)
    reasons.append("The sources differ in kind or uploader and the dates agree.")
    return Strength("corroborated", reasons)
