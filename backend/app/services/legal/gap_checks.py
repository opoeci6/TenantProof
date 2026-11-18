"""The deterministic gap checks (WS4, step 5a). Pure: no I/O, no model, no database.

These run before the gaps agent and are the part of the gaps step that is reproducible: the same
case always yields the same specs. The agent adds what only reading the evidence can find; it
never re-creates one of these, because `services/legal/gaps.py` skips a spec whose gap already
exists in any status.

A spec names the corpus entry its gap rests on (`source`), not a database id: this module knows
nothing about cases.
"""

from dataclasses import dataclass
from typing import Literal

# Corpus ids, resolved to this case's LegalSourceRow by services/legal/gaps.py.
FORM_J = "ns-form-j"
EVIDENCE_POLICY = "ns-evidence-policy-42-deadline"

Severity = Literal["blocking", "recommended"]


@dataclass(frozen=True)
class FieldFact:
    key: str
    label: str
    required: bool
    status: str


@dataclass(frozen=True)
class EvidenceFact:
    """One item, with the two metadata facts the photo checks need. They are computed by
    `services/strength.py` when the item is read, so this module does not repeat the rules."""

    evidence_id: str
    label: str
    kind: str
    no_metadata: bool = False  # a photo with no capture time
    edited: bool = False  # last saved by an image editor


@dataclass(frozen=True)
class EventFact:
    """One timeline event. `strength` is the one `services/strength.py` stored on it."""

    title: str
    strength: str  # corroborated | single_source | needs_support
    rejected: bool = False  # a caseworker rejected it: not part of the case


@dataclass(frozen=True)
class CaseFacts:
    attested: bool
    fields: tuple[FieldFact, ...]
    evidence: tuple[EvidenceFact, ...]
    events: tuple[EventFact, ...] = ()


@dataclass(frozen=True)
class GapSpec:
    severity: Severity
    title: str
    why: str
    how_to_fix: str
    field_key: str | None = None
    source: str | None = None  # a corpus entry id


def specs(facts: CaseFacts) -> list[GapSpec]:
    """Every gap the rules can find, blocking ones first. Titles are stable: a caseworker who
    dismisses one must not see it come back."""
    return [
        *_missing_fields(facts),
        *_unattested(facts),
        *_no_written_notice(facts),
        *_unsupported_events(facts),
        *_photo_originals(facts),
    ]


def _missing_fields(facts: CaseFacts) -> list[GapSpec]:
    return [
        GapSpec(
            severity="blocking",
            title=f"Missing from the form: {field.label}",
            why=(
                f"The application asks for {field.label.lower()}. It is empty on the form, and "
                "the package cannot be filed with a required field missing."
            ),
            how_to_fix="Ask the tenant, or take it from the lease and the messages.",
            field_key=field.key,
            source=FORM_J,
        )
        for field in facts.fields
        if field.required and field.status == "missing"
    ]


def _unattested(facts: CaseFacts) -> list[GapSpec]:
    if facts.attested:
        return []
    return [
        GapSpec(
            severity="blocking",
            title="The tenant must confirm their files",
            why=(
                "The package can only be filed once the tenant confirms the files are theirs and "
                "agrees to share them with the landlord and the program."
            ),
            how_to_fix="Send the intake link again; the tenant ticks the confirmation there.",
        )
    ]


def _no_written_notice(facts: CaseFacts) -> list[GapSpec]:
    if any(item.kind in ("letter", "messages") for item in facts.evidence):
        return []
    return [
        GapSpec(
            severity="recommended",
            title="Proof the landlord was told about the problem",
            why=(
                "Nothing in the evidence shows the landlord was told about the problem before the "
                "application."
            ),
            how_to_fix=(
                "Ask for the message or letter that told the landlord, or the date it was said in "
                "person."
            ),
        )
    ]


def support_title(event_title: str) -> str:
    return f"Support for: {event_title}"


def _unsupported_events(facts: CaseFacts) -> list[GapSpec]:
    """An event no verified source supports is only the tenant's word. Ask for something that
    shows it: the officer weighs how reliable each piece of evidence is (Evidence Policy #42)."""
    return [
        GapSpec(
            severity="recommended",
            title=support_title(event.title),
            why=(
                f"Nothing in the evidence backs up “{event.title}”, so it rests on the "
                "tenant's word alone and carries less weight."
            ),
            how_to_fix=(
                "Ask the tenant for a photo, message, receipt or letter that shows it, with a date."
            ),
            source=EVIDENCE_POLICY,
        )
        for event in facts.events
        if event.strength == "needs_support" and not event.rejected
    ]


def _photo_originals(facts: CaseFacts) -> list[GapSpec]:
    """A photo with no capture time, or one saved by an editor, is easier to doubt. Ask for the
    original: the officer weighs how reliable each piece of evidence is (Evidence Policy #42)."""
    out: list[GapSpec] = []
    for item in facts.evidence:
        if item.kind != "photo":
            continue
        if item.no_metadata:
            out.append(
                GapSpec(
                    severity="recommended",
                    title=f"Original photo for {item.label}",
                    why=(
                        f"{item.label} has no capture time, so it carries less weight. The "
                        "officer weighs how reliable each piece of evidence is."
                    ),
                    how_to_fix=(
                        "Ask the tenant to upload the original from their phone's camera roll."
                    ),
                    source=EVIDENCE_POLICY,
                )
            )
        if item.edited:
            out.append(
                GapSpec(
                    severity="recommended",
                    title=f"Unedited version of {item.label}",
                    why=(
                        f"{item.label} was last saved by an image editor. An unedited original is "
                        "easier to trust."
                    ),
                    how_to_fix=(
                        "Ask for the original file, or record what was changed (for example, "
                        "cropping only)."
                    ),
                    source=EVIDENCE_POLICY,
                )
            )
    return out
