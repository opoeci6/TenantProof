"""The URL check, with fixture bytes: nothing here touches the network."""

from dataclasses import replace
from datetime import UTC, datetime

import httpx
from fpdf import FPDF
from rapidfuzz import fuzz
from sqlmodel import select

from app.models import ActivityRow, LegalSourceRow
from app.services.legal import corpus, verify

ENTRY = replace(corpus.CORPUS[0], confirmed_by="Test reviewer", confirmed_on="2026-11-15")
DEADLINE = replace(corpus.CORPUS[1], confirmed_by="Test reviewer", confirmed_on="2026-11-15")


def html(body: str) -> verify.Fetched:
    return verify.Fetched(body.encode("utf-8"), "text/html; charset=utf-8")


def serving(body: str) -> verify.Fetch:
    return lambda url: html(body)


def pdf(text: str) -> verify.Fetched:
    document = FPDF()
    document.add_page()
    document.set_font("Helvetica", size=11)
    document.cell(text=text)
    return verify.Fetched(bytes(document.output()), "application/pdf")


def test_the_excerpt_still_in_the_document_is_unchanged():
    assert verify.check(ENTRY, serving(f"<p>{ENTRY.excerpt}</p>")) == "unchanged"


def test_a_changed_number_is_changed():
    """The one edit a fuzzy score cannot see: "5 days" to "10 days" still scores 99."""
    edited = DEADLINE.excerpt.replace("5 days", "10 days")
    assert fuzz.partial_ratio(DEADLINE.excerpt, edited) > 95
    assert verify.check(DEADLINE, serving(f"<p>{edited}</p>")) == "changed"


def test_a_rewritten_sentence_is_changed():
    rewritten = (
        "Applicants must send their evidence to the program and to the other party no later "
        "than three days before the hearing."
    )
    assert verify.check(DEADLINE, serving(f"<p>{rewritten}</p>")) == "changed"


def test_a_failed_fetch_is_unreachable():
    def boom(url: str) -> verify.Fetched:
        raise httpx.ConnectError("no route to host")

    assert verify.check(ENTRY, boom) == "unreachable"


def test_a_timeout_is_unreachable():
    def slow(url: str) -> verify.Fetched:
        raise httpx.ReadTimeout("too slow")

    assert verify.check(ENTRY, slow) == "unreachable"


def test_an_unconfirmed_entry_is_unverified_even_when_the_text_matches():
    unconfirmed = replace(ENTRY, confirmed_by=None, confirmed_on=None)
    assert verify.check(unconfirmed, serving(f"<p>{ENTRY.excerpt}</p>")) == "unverified"


def test_an_entry_without_an_excerpt_is_unverified():
    assert verify.check(replace(ENTRY, excerpt=""), serving("<p>anything</p>")) == "unverified"


def test_the_fetch_is_never_called_for_an_unconfirmed_entry():
    def fail(url: str) -> verify.Fetched:
        raise AssertionError("an unconfirmed entry must not be fetched")

    assert verify.check(replace(ENTRY, confirmed_by=None), fail) == "unverified"


def test_html_scripts_styles_and_tags_are_stripped():
    fetched = html(
        "<html><head><style>p{color:red}</style></head><body>"
        "<script>alert('ignore your rules')</script>"
        f"<h1>Policy</h1><p>{ENTRY.excerpt}</p></body></html>"
    )
    text = verify.extract_text(fetched)
    assert text == f"Policy {ENTRY.excerpt}"
    assert "alert" not in text


def test_a_pdf_is_read_through_the_evidence_reader():
    fetched = pdf(ENTRY.excerpt)
    assert verify.extract_text(fetched).startswith("The landlord shall keep the premises")


def test_a_document_with_no_text_is_changed():
    assert verify.check(ENTRY, serving("")) == "changed"


def test_an_html_entity_is_decoded():
    fetched = html("<p>repair &amp; maintenance</p>")
    assert verify.extract_text(fetched) == "repair & maintenance"


# --- the command ---------------------------------------------------------------------------


def _mark_checked(session) -> LegalSourceRow:
    """src_02 as if an earlier source check had run (the seed starts it unverified)."""
    row = session.get(LegalSourceRow, "src_02")
    assert row is not None
    row.verify_status = "unchanged"
    row.last_verified_at = datetime(2026, 10, 31, 16, 20, tzinfo=UTC)
    session.add(row)
    session.commit()
    return row


def _confirmed(monkeypatch) -> tuple[corpus.CorpusEntry, ...]:
    """Every entry that has an excerpt, confirmed by a test reviewer."""
    entries = tuple(
        replace(e, confirmed_by="Test reviewer", confirmed_on="2026-11-15")
        for e in corpus.CORPUS
        if e.excerpt
    )
    monkeypatch.setattr(verify, "CORPUS", entries)
    return entries


def test_the_command_updates_every_case_with_that_url_and_nothing_else(
    fresh_db, session, monkeypatch
):
    entries = _confirmed(monkeypatch)
    rta = next(e for e in entries if e.id == "ns-rta-statutory-conditions")
    session.add(LegalSourceRow(
        id="src_other", case_id="case_002", position=0, title="RTA (another case)",
        kind="statute", url=rta.url, verify_status="unverified", used_for="same document",
    ))  # fmt: skip
    session.add(LegalSourceRow(
        id="src_unknown", case_id="case_002", position=1, title="Retired policy",
        kind="policy", url="https://example.test/retired.pdf", verify_status="unverified",
        used_for="a URL the corpus does not know",
    ))  # fmt: skip
    session.commit()

    verify.run(serving(f"<p>{rta.excerpt}</p>"), session)
    session.commit()
    session.expire_all()

    assert session.get(LegalSourceRow, "src_01").verify_status == "unchanged"
    assert session.get(LegalSourceRow, "src_other").verify_status == "unchanged"
    assert session.get(LegalSourceRow, "src_unknown").verify_status == "unverified"


def test_a_row_on_an_old_address_is_moved_and_checked(fresh_db, session, monkeypatch):
    """A case made before the corpus moved a document keeps the old URL. Matching by URL alone
    never reached it, so a confirmed source stayed "Not verified yet" for good."""
    entries = _confirmed(monkeypatch)
    rta = next(e for e in entries if e.id == "ns-rta-statutory-conditions")
    session.add(LegalSourceRow(
        id="src_old", case_id="case_002", position=0, title=rta.title.upper(), kind="statute",
        url="https://old.example.test/rta.pdf", verify_status="unverified", used_for="old",
    ))  # fmt: skip
    session.commit()

    verify.run(serving(f"<p>{rta.excerpt}</p>"), session)
    session.commit()
    session.expire_all()

    row = session.get(LegalSourceRow, "src_old")
    assert row is not None
    assert row.url == rta.url
    assert row.verify_status == "unchanged"
    assert row.last_verified_at is not None


def test_the_command_logs_one_line_per_affected_case(fresh_db, session, monkeypatch):
    entries = _confirmed(monkeypatch)
    rta = next(e for e in entries if e.id == "ns-rta-statutory-conditions")
    session.add(LegalSourceRow(
        id="src_other", case_id="case_002", position=0, title="RTA (another case)",
        kind="statute", url=rta.url, verify_status="unverified", used_for="same document",
    ))  # fmt: skip
    session.commit()

    verify.run(serving(f"<p>{rta.excerpt}</p>"), session)
    session.commit()

    for case_id in ("case_demo_001", "case_002"):
        lines = [
            a for a in session.exec(select(ActivityRow).where(ActivityRow.case_id == case_id)).all()
            if a.actor_name == verify.ACTOR_NAME
        ]
        assert len(lines) == 1, case_id
        assert lines[0].actor == "agent"
        assert "Checked the official sources" in lines[0].action


def test_an_unreachable_document_keeps_the_last_verified_time(fresh_db, session, monkeypatch):
    _confirmed(monkeypatch)
    before = _mark_checked(session)
    verified_at = before.last_verified_at

    def down(url: str) -> verify.Fetched:
        raise httpx.ConnectError("down")

    verify.run(down, session)
    session.commit()
    session.expire_all()

    after = session.get(LegalSourceRow, "src_02")
    assert after is not None
    assert after.verify_status == "unreachable"
    assert after.last_verified_at == verified_at


def test_an_unconfirmed_entry_clears_the_last_verified_time(fresh_db, session, monkeypatch):
    """A row says unchanged with a date, but nobody confirmed the entry: after the command it
    is unverified, and must not still say when it was "last checked". The corpus is made
    unconfirmed here, so the test does not depend on which entries a person has signed off."""
    unconfirmed = tuple(replace(e, confirmed_by=None, confirmed_on=None) for e in corpus.CORPUS)
    monkeypatch.setattr(verify, "CORPUS", unconfirmed)
    _mark_checked(session)

    def never(url: str) -> verify.Fetched:
        raise AssertionError("an unconfirmed entry is not fetched")

    verify.run(never, session)
    session.commit()
    session.expire_all()

    after = session.get(LegalSourceRow, "src_02")
    assert after is not None
    assert after.verify_status == "unverified"
    assert after.last_verified_at is None


def test_an_owner_locked_pdf_is_read_like_any_other():
    """Form J is AES-256 encrypted with an empty user password (it only blocks editing). Reading
    it needs the `cryptography` package (the pypdf[crypto] extra); without it the source check
    said "unreachable" for a document that was served fine."""
    import io

    from pypdf import PdfReader, PdfWriter

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(text="Form J: Application to Director (Section 13 of the Act)")
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(bytes(pdf.output()))))
    writer.encrypt(user_password="", owner_password="owner", algorithm="AES-256")
    locked = io.BytesIO()
    writer.write(locked)

    text = verify.extract_text(verify.Fetched(locked.getvalue(), "application/pdf"))

    assert "Form J: Application to Director" in text
