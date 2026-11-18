# Legal sources: the human confirmation checklist

**Status 2026-11-17:** all five entries are confirmed by a person. NineJZ356 checked entries 1 to 3
by hand against the official documents (PR #35, 2026-11-16). Lameda12 confirmed entries 4 and 5 on
2026-11-17 (issue #54): the full Form J title line (edition 2024-01) and Regulations s. 25A(2). The
confirmation of Form J covers its **title line and URL only, not field coverage**: the draft is
partial (see below). An agent may not confirm a legal source, so confirming is a person's job: open
each URL, check the section, the form name and the wording, then fill in `confirmed_by` and
`confirmed_on` on the entry in `corpus.py`.

Until an entry has a `confirmed_by`, the app reports it as `unverified` whatever the URL check
finds, and the caseworker sees that in the sources list. Nothing else in the app depends on it:
the deadline comes from the rule attached to the evidence policy, not from the confirmation.

How to confirm one entry (about 20 minutes for the five):

1. Open the URL below in a browser.
2. Find the locator (`section 9(1)`, `page 4`).
3. Read the excerpt in `corpus.py` against the document. It must be word for word, with the
   PDF's line breaks joined by single spaces. Fix it if it is not; never paraphrase it.
4. Set `confirmed_by="<your name>"` and `confirmed_on="<today's date>"` on that entry.
5. Run `uv run python -m app.services.legal.verify` (see `verify.py`); a confirmed entry whose
   text still matches the published document comes back `unchanged`.

## The entries

| # | Entry id | What it is | URL | Locator | Confirmed? by whom |
|---|---|---|---|---|---|
| 1 | `ns-rta-statutory-conditions` | Residential Tenancies Act (Nova Scotia), statutory conditions | https://nslegislature.ca/sites/default/files/legc/statutes/residential%20tenancies.pdf | section 9(1), statutory condition 1 (Condition of Premises) | yes, NineJZ356, 2026-11-16 |
| 2 | `ns-evidence-policy-42-deadline` | Evidence Policy #42, the 5-day submission deadline | https://www.novascotia.ca/sites/default/files/documents/1-1319/evidence-policy-42-residential-tenancies-en.pdf | page 1, Details | yes, NineJZ356, 2026-11-16 |
| 3 | `ns-evidence-policy-42-formats` | Evidence Policy #42, accepted formats | same document, `#page=4` | page 4, Types of Evidence | yes, NineJZ356, 2026-11-16 |
| 4 | `ns-form-j` | Application to Director (Form J), edition 2024-01 | https://www.novascotia.ca/sites/default/files/documents/2-419/form-j-application-director-en.pdf | pages F1 to F3 (the form), G1 to G4 (the guide) | yes, Lameda12, 2026-11-17 (title line and URL; the field coverage is partial) |
| 5 | `ns-rtr-25a` | Residential Tenancies Regulations, section 25A (the legal basis of the evidence deadline) | https://novascotia.ca/just/regulations/regs/rtgenrl.htm | section 25A(2) | yes, Lameda12, 2026-11-17 |

## What each excerpt says

Verbatim, as stored in `corpus.py` today. Check these against the documents.

**1. RTA, statutory condition 1.**

> The landlord shall keep the premises in a good state of repair and fit for habitation during the
> tenancy and shall comply with any statutory enactment or law respecting standards of health,
> safety or housing.

**2. Evidence Policy #42, deadline.**

> If applicants or respondents intend to introduce evidence, other than oral testimony, during
> their hearing to demonstrate or explain their case they must submit their evidence at least 5
> days before their hearing date.

**3. Evidence Policy #42, formats.**

> Digital documents must be in one of the following formats: Microsoft Office Word, Microsoft
> Office Excel or Adobe PDF

**4. Form J.** The published PDF carries an owner-password lock, but its text is readable (a
PDF viewer or `pdftotext` shows all 9 pages). The excerpt is its full title line on page F1,
edition 2024-01:

> Form J: Application to Director (Section 13 of the Residential Tenancies Act)

The PDF breaks the line after "Director"; the stored excerpt joins it with a single space.

**5. Regulations, section 25A(2).** The deadline in #2 is the Program's reading of this
regulation, so the regulation is the primary source:

> A submission under subsection (1) must be made in sufficient time so that each recipient
> receives it at least 5 days before the date of the hearing, unless a later time is permitted
> under subsection (6).

The same section says when a submission counts as received: by registered mail, express post or
courier, **on the 3rd day after it was sent** (25A(3B)); by e-mail, the same day if sent before
4:00 p.m., **the next day** if sent at or after 4:00 p.m. (25A(3C)). The Director may allow
evidence later, up to and during the hearing (25A(6)); the Program says this is for exceptional
circumstances only (Policy #42, page 2).

The date the app shows is therefore a **must be received by** date, not a send-by date. Evidence
sent by registered mail, express post or courier has to be sent at least 8 days before the
hearing. Policy #42 (page 3) says the same about registered mail, express post, courier and
e-mail; it does not give a received-by rule for ordinary mail. The excerpt is stored unconfirmed
and the Regulations consolidation on novascotia.ca is unofficial ("amended to N.S. Reg. 76/2025,
effective April 30, 2025"), so check the Royal Gazette Part II for later amendments.

## Decisions a person should look at

- **The Form J draft is partial.** The app drafts seven tenant-applicant fields (`tenant_name`,
  `landlord_name`, `landlord_address`, `rental_address`, `tenancy_start`, `monthly_rent`,
  `remedy`). It is **not a complete Form J**: the caseworker completes the rest on the real form,
  and the UI and the package call it "Partial Form J draft: caseworker completes the rest". The
  labels are ours, plain-English versions. Not drafted, so the caseworker fills them in: the
  address split into number, name, apartment, city, province and postal code; mailing
  addresses; phone and e-mail with the consent box; the move-in date apart from a fixed-term
  start; weekly rent; the notice-to-quit, lease and deposit questions; several requests at once
  (Repairs, Compliance with a lease or the Act, Payment of money, ...) each with its own
  explanation; the signature and date. **F4 (the landlord's requests) is out of scope**: the app
  drafts tenant applications only. Full coverage is tracked in the issue "Full Form J field
  coverage"; the comparison behind this list is leozhang5867's comment on #54.
- **Policy #28 ("Hearings Procedures and Conduct") is not in the corpus.** The URL the seed data
  carries
  (`.../ans-residential-tenancies-policy-28-residential-tenancies-hearings-procedures-and-conduct.pdf`)
  now redirects to the policy index, and the current index lists six policies, none of them
  #28. The nearest current document is Policy #40, Format of Hearings. Rather than cite a
  document that no longer exists, WS4 leaves it out: the demo case's `src_04` row keeps its dead
  URL and stays `unverified`. Retiring or repointing that row belongs to whoever owns the seed
  data, not to WS4.
- **What the Program accepts is narrower than what this app takes in** (Policy #42, page 4).
  Digital documents: Microsoft Word, Microsoft Excel or Adobe PDF. Digital photos: jpeg, jpg, png,
  bmp or pdf. Video and audio: wmv, mpg, mpeg, m4v or mp4. All digital evidence goes through
  Nova Scotia's online service.
  - **HEIC is not accepted.** The app takes HEIC from phones, and the package files a JPG made from
    it instead; no `.heic` file is ever in the package.
  - **No zip or ARC files** ("all digital files must be in an uncompressed format"). The package the
    app builds is a zip for the caseworker's records; the files inside it are what get uploaded, one
    by one. Nothing in the app tells anyone to submit the zip itself.
  - **No links to cloud drives.** Evidence stored on Google Drive, OneDrive, Dropbox and similar
    services is not accepted.
  - The app's TXT-to-PDF copy matches "text messages must be submitted digitally or printed". The
    gaps are Word and Excel (the app refuses them) and video and audio (it takes none). That is a
    WS1 decision, noted here so it is not a surprise at the demo.
- **Evidence sent by registered mail, express post, courier or e-mail is counted from the day it
  counts as received**, not sent (see #5). Only those three mail services get the 3rd-day rule.
