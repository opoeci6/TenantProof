"""The corpus is data, so these check its invariants, not its wording: whether the words match
the official documents is a person's job (`services/legal/README.md`)."""

from app.services import dates
from app.services.legal import corpus


def test_entry_ids_are_unique():
    ids = [e.id for e in corpus.CORPUS]
    assert len(ids) == len(set(ids))


def test_urls_are_unique():
    urls = [e.url for e in corpus.CORPUS if e.url is not None]
    assert len(urls) == len(set(urls))


def test_the_deadline_rule_is_the_one_dates_uses():
    """The answer route reads `dates.EVIDENCE_DAYS_BEFORE_HEARING`, the rules step reads the
    corpus. If these ever differ, a case gets two different deadlines."""
    assert corpus.EVIDENCE_DAYS_BEFORE_HEARING == dates.EVIDENCE_DAYS_BEFORE_HEARING


def test_every_entry_says_where_its_text_sits():
    for entry in corpus.CORPUS:
        assert entry.locator.strip(), entry.id


def test_an_excerpt_is_one_plain_line():
    """The PDF's line breaks are joined by single spaces, so a stored excerpt can be compared
    with extracted text without normalising twice."""
    for entry in corpus.CORPUS:
        if not entry.excerpt:
            continue
        assert "\n" not in entry.excerpt, entry.id
        assert "  " not in entry.excerpt, entry.id
        assert entry.excerpt == entry.excerpt.strip(), entry.id


def test_the_form_entry_carries_the_fields_the_gaps_step_writes():
    form = next(e for e in corpus.CORPUS if e.kind == "form")
    assert [f.key for f in form.form_fields] == [
        "tenant_name", "landlord_name", "landlord_address", "rental_address",
        "tenancy_start", "monthly_rent", "remedy",
    ]  # fmt: skip
    assert all(f.label.strip() for f in form.form_fields)


def test_the_two_evidence_policy_entries_share_a_document_but_not_a_url():
    """Both rows point at one PDF. Without the anchor they would be the same row."""
    urls = [e.url for e in corpus.CORPUS if (e.url or "").startswith(corpus.EVIDENCE_POLICY_URL)]
    assert len(urls) == 2
    assert urls[0] != urls[1]


def test_a_row_is_matched_by_url_before_title():
    entry = corpus.CORPUS[0]
    assert corpus.entry_for(entry.url, "a title nobody stored") is entry


def test_a_row_without_a_url_is_matched_by_title():
    form = next(e for e in corpus.CORPUS if e.kind == "form")
    assert form.url is not None  # the seed has no URL; the corpus does
    assert corpus.entry_for(None, form.title) is form


def test_an_unknown_url_does_not_fall_back_to_the_title():
    form = next(e for e in corpus.CORPUS if e.kind == "form")
    assert corpus.entry_for("https://example.test/other.pdf", form.title) is None


def test_every_entry_is_confirmed_by_a_person():
    """NineJZ356 checked the first three against the official documents on 2026-11-16 (PR #35);
    Lameda12 confirmed Form J (title line) and the Regulations entry on 2026-11-17 (issue #54)."""
    by_person = {e.id: e.confirmed_by for e in corpus.CORPUS}
    assert by_person == {
        "ns-rta-statutory-conditions": "NineJZ356",
        "ns-evidence-policy-42-deadline": "NineJZ356",
        "ns-evidence-policy-42-formats": "NineJZ356",
        "ns-rtr-25a": "Lameda12",
        "ns-form-j": "Lameda12",
    }
    assert all(e.excerpt and e.confirmed_on for e in corpus.CORPUS if e.confirmed_by)
