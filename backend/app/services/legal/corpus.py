"""The curated corpus of official sources (WS4, BUILD_PLAN section 5 "rules").

A typed list in code, not a table: the corpus changes with a commit and a review, and nothing
about it is per case. Each `LegalSourceRow` a case shows is matched to an entry here by URL (or
by title when the entry has no URL); `services/legal/rules.py` inserts the missing rows.

Two rules hold for every entry:

- **`excerpt` is a verbatim copy of the official text**, with the line breaks of the PDF joined
  by single spaces and nothing else changed. It is never paraphrased or translated. An entry
  whose text nobody has checked has `excerpt=""`, and an entry is only ever `"unchanged"` or
  `"changed"` once a person has confirmed it (`confirmed_by`).
- **`url` is the document the excerpt comes from.** Two entries may share one document (the
  evidence policy's deadline and its accepted formats do); the second carries a `#page=` anchor
  so the two rows stay distinguishable.

`services/legal/README.md` is the checklist a person works through to fill in `confirmed_by`.
"""

from dataclasses import dataclass
from typing import Literal

Kind = Literal["statute", "policy", "form"]


@dataclass(frozen=True)
class FormFieldSpec:
    """One field of the official form this app drafts against. `key` is ours, not the form's."""

    key: str
    label: str
    required: bool = True


@dataclass(frozen=True)
class CorpusEntry:
    id: str  # stable; the row id is built from it, so never change one
    title: str  # shown in the UI as the source's title
    kind: Kind
    url: str | None
    used_for: str  # one plain sentence: what this source is cited for
    locator: str  # where the excerpt sits: "section 9(1)", "page 4"
    excerpt: str  # verbatim official text, or "" until a person confirms it
    keywords: tuple[str, ...] = ()  # extra search words for search_legal_sources
    confirmed_by: str | None = None  # the person who opened the URL and checked the excerpt
    confirmed_on: str | None = None  # ISO date
    rule: dict[str, int] | None = None  # a number the code reads, e.g. the deadline
    form_fields: tuple[FormFieldSpec, ...] = ()  # only the form entry has these


RTA_URL = "https://nslegislature.ca/sites/default/files/legc/statutes/residential%20tenancies.pdf"
EVIDENCE_POLICY_URL = (
    "https://www.novascotia.ca/sites/default/files/documents/1-1319/"
    "evidence-policy-42-residential-tenancies-en.pdf"
)
# The `beta.` host redirects here; the bare novascotia.ca host serves a 404 for this path.
FORM_J_URL = (
    "https://www.novascotia.ca/sites/default/files/documents/2-419/"
    "form-j-application-director-en.pdf"
)

RTR_URL = "https://novascotia.ca/just/regulations/regs/rtgenrl.htm"

# What the UI and the package call the form draft. The app drafts seven tenant-applicant fields,
# not the whole of Form J: the caseworker completes the rest on the real form.
FORM_J_DRAFT_LABEL = "Partial Form J draft: caseworker completes the rest"

# The form fields this app drafts. They follow the seeded demo case and the labels are ours. This
# is a PARTIAL draft: seven fields for a tenant applicant, not a complete Form J. The caseworker
# completes the rest on the real form (see README.md). F4 (the landlord's requests) is out of scope.
# TODO(full Form J coverage, issue "Full Form J field coverage"): not drafted here: the
# address split into number, name, apartment, city, province and postal code; mailing addresses;
# phone and e-mail with the consent box; the move-in date apart from a fixed-term start; weekly
# rent; the notice-to-quit, lease and deposit questions; several requests at once (Repairs,
# Compliance with a lease or the Act, Payment of money, ...) each with its own explanation; the
# signature and date.
FORM_J_FIELDS = (
    FormFieldSpec("tenant_name", "Tenant name"),
    FormFieldSpec("landlord_name", "Landlord name"),
    FormFieldSpec("landlord_address", "Landlord address"),
    FormFieldSpec("rental_address", "Rental unit address"),
    FormFieldSpec("tenancy_start", "Tenancy start date"),
    FormFieldSpec("monthly_rent", "Monthly rent"),
    FormFieldSpec("remedy", "What the tenant is asking for"),
)

CORPUS: tuple[CorpusEntry, ...] = (
    CorpusEntry(
        id="ns-rta-statutory-conditions",
        title="Residential Tenancies Act (Nova Scotia), statutory conditions",
        kind="statute",
        url=RTA_URL,
        used_for="The landlord's duty to keep the premises in good repair.",
        locator="section 9(1), statutory condition 1 (Condition of Premises)",
        excerpt=(
            "The landlord shall keep the premises in a good state of repair and fit for "
            "habitation during the tenancy and shall comply with any statutory enactment or law "
            "respecting standards of health, safety or housing."
        ),
        keywords=("repair", "repairs", "mould", "mold", "maintenance", "habitable",
                  "landlord duty", "维修", "发霉"),
        confirmed_by="NineJZ356",  # checked by hand against the official document (PR #35)
        confirmed_on="2026-11-16",
    ),
    CorpusEntry(
        id="ns-evidence-policy-42-deadline",
        title="Residential Tenancies Program, Evidence Policy #42",
        kind="policy",
        url=EVIDENCE_POLICY_URL,
        used_for=(
            "Evidence must be received by the other party and the program at least 5 days "
            "before the hearing. Evidence sent by registered mail, express post or courier "
            "counts as received on the 3rd day after it is sent. "
            "The officer may allow late evidence in exceptional circumstances."
        ),
        locator="page 1, Details",
        excerpt=(
            "If applicants or respondents intend to introduce evidence, other than oral "
            "testimony, during their hearing to demonstrate or explain their case they must "
            "submit their evidence at least 5 days before their hearing date."
        ),
        keywords=("deadline", "due date", "submit evidence", "5 days", "five days", "hearing"),
        rule={"evidence_days_before_hearing": 5},
        confirmed_by="NineJZ356",  # the 5 matches "at least 5 days" (PR #35)
        confirmed_on="2026-11-16",
    ),
    CorpusEntry(
        id="ns-rtr-25a",
        title="Residential Tenancies Regulations, section 25A: submitting evidence",
        kind="statute",
        url=RTR_URL,
        used_for=(
            "The legal basis of the evidence deadline: it must be received at least 5 days before "
            "the hearing. Registered mail, express post or courier counts as received on the 3rd "
            "day after sending; e-mail the same day if sent before 4 p.m., the next day if sent at "
            "or after 4 p.m.; and the Director may allow late evidence."
        ),
        locator="section 25A(2)",
        excerpt=(
            "A submission under subsection (1) must be made in sufficient time so that each "
            "recipient receives it at least 5 days before the date of the hearing, unless a "
            "later time is permitted under subsection (6)."
        ),
        keywords=("regulation", "regulations", "25A", "received", "mail", "email", "e-mail",
                  "late evidence", "deadline", "5 days"),
        confirmed_by="Lameda12",
        confirmed_on="2026-11-17",
    ),
    CorpusEntry(
        id="ns-evidence-policy-42-formats",
        title="Residential Tenancies Program, Evidence Policy #42: accepted formats",
        kind="policy",
        # Same document as the entry above; the anchor keeps the two rows apart.
        url=f"{EVIDENCE_POLICY_URL}#page=4",
        used_for=(
            "Which file formats the program accepts for documents, and that text messages are "
            "submitted digitally or printed."
        ),
        locator="page 4, Types of Evidence",
        excerpt=(
            "Digital documents must be in one of the following formats: Microsoft Office Word, "
            "Microsoft Office Excel or Adobe PDF"
        ),
        keywords=("format", "pdf", "word", "excel", "text messages", "photo", "scan"),
        confirmed_by="NineJZ356",  # checked by hand against page 4 (PR #35)
        confirmed_on="2026-11-16",
    ),
    CorpusEntry(
        id="ns-form-j",
        title="Application to Director (Form J)",
        kind="form",
        url=FORM_J_URL,
        used_for=(
            "The application the draft is based on: a partial draft of seven tenant-applicant "
            "fields. The caseworker completes the rest of the form."
        ),
        locator="page 1, applicant and dispute details",
        # The full title line of the form, edition 2024-01 (page F1). The confirmation covers this
        # line only, not the field coverage: the draft is partial (see FORM_J_FIELDS).
        excerpt=(
            "Form J: Application to Director (Section 13 of the Residential Tenancies Act)"
        ),
        confirmed_by="Lameda12",
        confirmed_on="2026-11-17",
        keywords=("form", "application", "form j", "apply", "director", "file"),
        form_fields=FORM_J_FIELDS,
    ),
)


def _rule(name: str) -> int:
    """The value of a named rule. Exactly one entry may carry it."""
    found = [e.rule[name] for e in CORPUS if e.rule and name in e.rule]
    if len(found) != 1:
        raise ValueError(f"rule {name!r} must appear on exactly one corpus entry")
    return found[0]


# The one place the deadline comes from. The rules step, the hearing-date answer route and
# `priority.py` read this, so a change to the policy changes the deadline everywhere at once.
# `dates.EVIDENCE_DAYS_BEFORE_HEARING` is only the pure module's default; a test pins it to this.
EVIDENCE_DAYS_BEFORE_HEARING = _rule("evidence_days_before_hearing")


def by_url(url: str | None) -> CorpusEntry | None:
    if url is None:
        return None
    return next((e for e in CORPUS if e.url is not None and e.url == url), None)


def by_id(entry_id: str) -> CorpusEntry | None:
    return next((e for e in CORPUS if e.id == entry_id), None)


def by_title(title: str) -> CorpusEntry | None:
    wanted = title.strip().casefold()
    return next((e for e in CORPUS if e.title.casefold() == wanted), None)


def entry_for(url: str | None, title: str) -> CorpusEntry | None:
    """The entry a stored row belongs to: by URL, or by title when the row has no URL (Form J
    was seeded without one)."""
    entry = by_url(url)
    if entry is not None:
        return entry
    return by_title(title) if url is None else None
