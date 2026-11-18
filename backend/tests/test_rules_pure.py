"""dates.py, strength.py and verifier.py are pure (BUILD_PLAN section 6)."""

import pytest

from app.schemas import EventDate
from app.services import dates, strength, verifier


def d(value: str | None, precision: str = "day", basis: str = "exif") -> EventDate:
    return EventDate(value=value, precision=precision, basis=basis)  # type: ignore[arg-type]


# --- dates


def test_choose_date_follows_basis_priority() -> None:
    picked = dates.choose_date(
        [d("2026-08-14", basis="filename"), d("2026-08-15", basis="exif"),
         d("2026-08-16", basis="message_timestamp")]
    )  # fmt: skip
    assert picked.basis == "message_timestamp"


def test_choose_date_prefers_day_over_month_and_the_first_on_a_tie() -> None:
    assert dates.choose_date([d("2026-09", "month", "exif"), d("2026-09-02")]).precision == "day"
    assert dates.choose_date([d("2026-09-02"), d("2026-09-03")]).value == "2026-09-02"


def test_choose_date_uses_unknown_only_when_nothing_else() -> None:
    unknown = d(None, "unknown", "message_timestamp")
    assert dates.choose_date([unknown, d("2026-09-02", basis="filename")]).value == "2026-09-02"
    assert dates.choose_date([unknown]).precision == "unknown"


@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        ([d("2026-08-14"), d("2026-08-15")], False),  # one day apart is allowed
        ([d("2026-08-14"), d("2026-08-16")], True),
        ([d("2026-09", "month"), d("2026-09-20")], False),
        ([d("2026-09", "month"), d("2026-10-01")], True),
        ([d("2026-08-14"), d(None, "unknown")], False),
        ([d("2026-08-14")], False),
    ],
)
def test_date_mismatch(candidates: list[EventDate], expected: bool) -> None:
    assert dates.date_mismatch(candidates) is expected


def test_date_inferred_only_for_filename() -> None:
    assert dates.date_inferred(d("2026-09", "month", "filename"))
    assert not dates.date_inferred(d("2026-09-01"))


# --- strength


def src(ev: str, kind: str = "photo", by: str = "tenant", cap: str | None = "t",
        software: str | None = None) -> strength.SourceFacts:
    return strength.SourceFacts(ev, kind, by, cap, software)


def test_no_sources_needs_support() -> None:
    assert strength.compute_strength([], mismatch=False).strength == "needs_support"


def test_one_source_is_single_source() -> None:
    assert strength.compute_strength([src("a")], mismatch=False).strength == "single_source"


def test_two_independent_sources_corroborate() -> None:
    out = strength.compute_strength([src("a"), src("b", kind="messages")], mismatch=False)
    assert out.strength == "corroborated" and out.reasons


def test_two_sources_of_the_same_kind_and_uploader_are_not_independent() -> None:
    out = strength.compute_strength([src("a"), src("b")], mismatch=False)
    assert out.strength == "single_source"
    assert strength.compute_strength([src("a"), src("a", kind="messages")], mismatch=False
                                     ).strength == "single_source"  # same evidence item


def test_uploader_difference_counts_as_independent() -> None:
    sources = [src("a"), src("b", by="caseworker")]
    assert strength.compute_strength(sources, mismatch=False).strength == "corroborated"


def test_edited_or_mismatched_sources_never_corroborate() -> None:
    edited = [src("a", software="Adobe Lightroom Mobile"), src("b", kind="messages")]
    assert strength.compute_strength(edited, mismatch=False).strength == "single_source"
    only_edited = [src("a", software="Adobe Photoshop")]
    assert strength.compute_strength(only_edited, mismatch=False).strength == "needs_support"
    two = [src("a"), src("b", kind="messages")]
    assert strength.compute_strength(two, mismatch=True).strength == "needs_support"


def test_pdf_producer_is_not_an_image_editor() -> None:
    assert not src("a", kind="lease", software="Adobe Acrobat").edited
    assert src("a", software="GIMP 2.10").edited


def test_flags() -> None:
    sources = [src("a", cap=None, software="Snapseed"), src("b", kind="lease")]
    flags = strength.event_flags(sources, d("2026-09", "month", "filename"), mismatch=True)
    assert {f.type for f in flags} == {
        "no_metadata", "edited_software", "date_inferred", "date_mismatch"
    }
    assert strength.event_flags([src("a")], d("2026-09-01"), mismatch=False) == []


# --- verifier

PAGES = {
    "page 1": "The term of this lease begins on May 1, 2026.\nRent is $1,150 per month.",
    "page 2": "Signed by the landlord <J. Smith>.",
}


def test_exact_and_fuzzy_excerpts_pass() -> None:
    verify = verifier.verify_citation
    assert verify("The term of this lease begins on May 1, 2026.", "page 1", PAGES).ok
    assert verify("the term of this  lease begins on may 1 2026", "page 1", PAGES).ok
    v = verifier.verify_citation("Rent is $1,150 per month", "PAGE 1", PAGES)  # locator case
    assert v.ok and v.score >= verifier.MIN_SCORE


def test_invented_excerpt_is_rejected() -> None:
    v = verifier.verify_citation("The landlord admits the roof leaked in June.", "page 1", PAGES)
    assert not v.ok and v.reason == "citation rejected: excerpt not found in the evidence"


def test_unknown_locator_is_rejected() -> None:
    v = verifier.verify_citation("Rent is $1,150 per month", "page 9", PAGES)
    assert not v.ok and "locator" in (v.reason or "")


def test_excerpt_must_be_found_at_its_own_locator() -> None:
    assert not verifier.verify_citation("Rent is $1,150 per month", "page 2", PAGES).ok


def test_short_excerpts_prove_nothing() -> None:
    v = verifier.verify_citation("rent", "page 1", PAGES)
    assert not v.ok and "too short" in (v.reason or "")


def test_escaped_angle_brackets_match() -> None:
    assert verifier.verify_citation("landlord <J. Smith>", "page 2", PAGES).ok
    escaped = verifier.verify_citation("landlord &lt;J. Smith>", "page 2", PAGES)
    assert escaped.ok and escaped.matched == "landlord <J. Smith>"


def test_an_excerpt_that_contains_the_page_plus_invented_words_is_rejected() -> None:
    short = {"page 1": "Landlord: Harbour Ltd."}
    lie = "Landlord: Harbour Ltd. The landlord admits the ceiling leak is his fault, no fix."
    v = verifier.verify_citation(lie, "page 1", short)
    assert not v.ok and "not found" in (v.reason or "")


def test_a_passing_citation_returns_the_text_found_in_the_page() -> None:
    """The saved quote is the page's own words, not the model's."""
    page = {"message 2": "[2026-08-16 18:30] Landlord: I will send someone to look at it."}
    flipped = "Landlord: I will not send someone to look at it."
    v = verifier.verify_citation(flipped, "message 2", page)
    assert v.ok and v.score < 100
    assert v.matched != flipped and "not" not in v.matched
    assert v.matched in page["message 2"] and v.locator == "message 2"


def test_the_matched_span_keeps_the_original_case_and_spacing() -> None:
    pages = {"Page 1": "Intro.\nRent  is   $1,150\tper month. More."}
    v = verifier.verify_citation("rent is $1,150 per month", "page 1", pages)
    assert v.ok and v.matched == "Rent  is   $1,150\tper month" and v.locator == "Page 1"
