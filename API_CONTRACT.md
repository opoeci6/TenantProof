# TenantProof — UI ↔ Backend Data Contract (draft v0.2)

**Positioning (v0.2):** legal clinics and tenant advocacy organizations are the customer; tenants are the beneficiaries.

| Who | What they do | Cost |
|---|---|---|
| **Tenant** | Opens an intake link on their phone, uploads photos, lease and messages in their own language. | Free |
| **Caseworker** (primary user) | Reviews the agent's timeline and form draft, adds professional notes, approves anything sent to the tenant, manages a caseload. | Clinic pays a subscription, or it's covered by government or foundation funding |

The UI prototype (`tenantproof-prototype.html`) is built against this contract.
It runs on mock data through one function, `request(method, path, body)`.
Set `USE_MOCK = false` and point `API_BASE` at the backend, and the same screens call the real API.

All demo data is **synthetic**. Times are ISO 8601 with offset. IDs are opaque strings.
v0.1 (individual-tenant version) is kept as `API_CONTRACT-v1-individual.md`.

---

## Design rules the UI depends on

1. **No AI confidence percentages.** Evidence strength and caseload priority are categories computed by
   **deterministic rules** (independent sources, metadata, date agreement, deadlines, blocking items).
2. **Every AI-produced item carries `sources`.** Items without a file + location are not shown as fact.
3. **Dates carry precision and basis** (`day` / `month` / `unknown`, from EXIF, message time, document, filename, user).
4. **Long work is a job.** Analysis is a `Run` with six steps. The UI polls; steps can be `failed`, `incomplete` or `needs_input`.
5. **The caseworker gates the output.** The form narrative uses only events with `review_status`
   `accepted` or `edited`. Rejected events never reach the form.
6. **The agent never decides legal questions.** The remedy is the tenant's choice, recorded by the caseworker.
   Legal statements cite a `LegalSource` with a verification status.
7. **Nothing leaves the clinic without approval.** The agent drafts requests to the tenant in the tenant's
   language; a caseworker approves before anything is sent.
8. **Every action is logged.** Agent, tenant and caseworker actions all go to the case activity log.
9. **Privacy:** EXIF/GPS is read for the evidence log, then stripped before any file is sent to the model.
   Tenants only see their own intake page, never the caseworker workspace.

---

## Endpoints

### Caseworker app

The applicant-role request, role-specific restrictions and draft PDF endpoint documented here take effect when [PR #154](https://github.com/opoeci6/TenantProof/pull/154) ships.

| Method | Path | Returns | Notes |
|---|---|---|---|
| POST | `/auth/login` | `User` | Body: `{ email, password }`. Sets the session cookie (httpOnly, SameSite=Lax). 401 `Wrong email or password` for an unknown email and a wrong password alike; 429 when rate limited. |
| POST | `/auth/logout` | `{ ok }` | Ends the session and clears the cookie. |
| GET | `/me` | `User` | Signed-in caseworker (401 when signed out). |
| GET | `/cases` | `CaseSummary[]` | Caseload, already sorted by priority (rule-based). |
| POST | `/cases` | `Case` | Creates a case in the signed-in caseworker's org, assigned to them. Body: `ApplicantCaseCreate` `{ applicant_role?, tenant_name, tenant_language, unit, title, idempotency_key? }`; `tenant_language` is one of `en`, `zh`, `fr`, `es`, `ru`, `hi`. `applicant_role` accepts `tenant` or `landlord` and defaults to `tenant` for callers that omit it. Landlord cases prepare a manual draft immediately. Tenant cases initialize the core form fields during analysis. Optional `idempotency_key`: 1 to 64 characters matching `[A-Za-z0-9._:-]+`, scoped per org. If a case in the caller's org already carries that key, the existing `Case` is returned (HTTP 200) and no duplicate case, form fields or "Created the case" activity line are made; an invalid body (422) never binds the key, so a corrected retry with the same key creates the case (#202). 422 `{ detail: { reasons } }` for invalid values or extra fields. See CONTRACT_CHANGES #35 and #49. |
| GET | `/cases/{case_id}` | `Case` | Includes the stored `applicant_role`, and `has_run` / `has_package`: whether the case has any run and any built package (#218). |
| POST | `/cases/{case_id}/intake-link` | `Intake` | Creates or re-sends the tenant link. 409 for landlord cases. |
| GET | `/cases/{case_id}/evidence` | `EvidenceItem[]` | |
| POST | `/cases/{case_id}/evidence` | `EvidenceItem` | Caseworker upload, `multipart/form-data`, field `file`. |
| GET | `/cases/{case_id}/privacy` | `PrivacyView` | The personal details of the case and the placeholder the AI model sees for each (issue #60). See "Personal details and the AI model". |
| POST | `/cases/{case_id}/privacy/terms` | `PrivacyView` | Body `PrivacyTermCreate` `{ value, action, role? }`: `hide` a value the finder missed, or `show` (send as written) a value it found. A second choice for the same value (any case or spacing) replaces the first. 422 `{ detail: { reasons } }` for a bad body. Logged without the value. |
| DELETE | `/cases/{case_id}/privacy/terms/{term_id}` | `PrivacyView` | Undoes a choice. 404 for a term of another case or org. Logged. |
| GET | `/cases/{case_id}/evidence/{evidence_id}/model-text` | `EvidenceModelText` | The item's text pages as the model receives them: with placeholders when `PRIVACY_PLACEHOLDERS` is on, as written when it is off. 404 for an item of another case or org. |
| GET | `/cases/{case_id}/evidence/{evidence_id}/preview` | image (`image/jpeg` or `image/png`) | The photo as the caseworker sees it: the copy made at upload without EXIF or GPS, never the original (a HEIC's copy is a JPEG). `Cache-Control: private, no-cache` with `ETag: "<sha256 of the copy>"`, so the browser checks the session on every view and an unchanged copy answers 304 without its bytes. 404 for another org's case, an item in another case, or an item with `has_preview: false`; 401 when not signed in (CONTRACT_CHANGES #97). |
| POST | `/cases/{case_id}/runs` | `Run` | Starts tenant case analysis and returns immediately with `status: "running"`. Landlord cases return 409 and use the manual draft. |
| GET | `/cases/{case_id}/runs/latest` | `Run` | 404 if none. Unchanged by #218: the UI reads `Case.has_run` and does not ask when it is false. |
| GET | `/runs/{run_id}` | `Run` | UI polls every ~1 s until `status` is not `running`. |
| POST | `/runs/{run_id}/answer` | `Run` | Body: `{ answer }`, a string of 1 to 500 characters. Resumes a run paused by `ask_caseworker` and returns it with `status: "running"`. 404 for an unknown run or another org's run, 409 when the run is not `needs_input` or this pause was already answered, 422 `{ detail: { reasons } }` when the answer is not a valid date for a date question (the run stays paused). See "Answering a paused run". |
| GET | `/cases/{case_id}/timeline` | `TimelineEvent[]` | Sorted by date. |
| PATCH | `/timeline/{event_id}` | `TimelineEvent` | Body: `{ review_status, title?, description?, date?, caseworker_note? }` |
| GET | `/cases/{case_id}/gaps` | `Gap[]` | |
| PATCH | `/gaps/{gap_id}` | `Gap` | Body: `{ status, resolution_note?, value? }`. New field values use the form validation below. After PR #143 ships, reopening a resolved gap whose field is covered by its Form J alternative returns 422 with a plain reason. |
| POST | `/gaps/{gap_id}/tenant-request` | `Gap` | Caseworker approved the drafted message. Sends it via the intake link; gap → `waiting_tenant`. Optional body `{ message, message_en }`: the request as the caseworker edited it (CONTRACT_CHANGES #37). |
| POST | `/gaps/{gap_id}/tenant-request/translate` | `{ message }` | Body: `{ message_en }`. Translates the caseworker's English into the tenant's language with one model call. Saves nothing (CONTRACT_CHANGES #37). |
| POST | `/cases/{case_id}/translate-excerpt` | `{ text_en }` | Body: `{ excerpt, language? }` (excerpt 1–3000 characters; language defaults to the case's tenant language). One model call for a quote not seen before; the result is cached by language + excerpt (`excerpt_translation`) and returned without a model call on a repeat. An excerpt is returned unchanged when the language is English; otherwise any excerpt, ASCII or not, is translated (a quote already in English comes back as written). 404 for another org's case, 503 `{ detail: { reasons } }` when the model is unavailable. A reading aid only: never cited, filed, or used to check a quote (CONTRACT_CHANGES #96). |
| GET | `/cases/{case_id}/sources` | `LegalSource[]` | |
| GET | `/cases/{case_id}/form-draft` | `FormDraft` | Narrative rebuilt from accepted events on every GET. See [Form J draft fields](#form-j-draft-fields) for the F1–F3 rollout (PR #143). |
| GET | `/cases/{case_id}/form-draft/pdf` | PDF (`application/pdf`) | Partial unsigned draft for landlord applicants. Lists the case’s supporting files by exhibit label, filename and SHA-256. The index identifies originals for reference when preparing filing copies. Signed-in caseworker in the case org only; 409 for tenant applications. Sends `Cache-Control: private, no-store`. |
| PATCH | `/cases/{case_id}/form-draft/fields/{key}` | `FormField` | Body `{ value }`. A nonempty value becomes `user_entered`. An empty value clears the field to `missing` with `value: null`. 404 for keys outside the case’s applicant role. See [Form J draft fields](#form-j-draft-fields) for accepted keys and validation. |
| GET | `/cases/{case_id}/activity` | `ActivityEntry[]` | Newest first. |
| POST | `/cases/{case_id}/package` | `Package` | Body: `{ attestations }`. 409 for landlord cases, blocking gaps, or missing tenant attestation. |
| GET | `/cases/{case_id}/package` | `Package` | Returns the latest built tenant package (or `?version=N`, `N >= 1`). 404 if no package is built, the version is unknown, or the case belongs to another org; 409 for landlord cases; 401 when signed out. Sends `Cache-Control: private, no-store`. Not logged (#200). Unchanged by #218: the UI reads `Case.has_package` and `applicant_role` and does not ask when there is nothing to read. |
| GET | `/cases/{case_id}/package/download` | zip (`application/zip`) | Streams a built tenant package. Query `version` (default: the latest); 404 if none is built, 409 for landlord cases. Headers: `Content-Disposition: attachment; filename="tenantproof-<case>-v<version>.zip"` and `X-Package-Sha256` (the zip's SHA-256). Images in the zip have their GPS data removed and a HEIC is filed as a JPG (CONTRACT_CHANGES). |
| GET | `/cases/{case_id}/package/files/{filename:path}` | file (`mimetypes` guess or `application/octet-stream`) | Downloads one file from a built tenant package zip. Landlord cases return 409. Query `version` (default: the latest). Serves only files listed in that version's `files` (exact, case-sensitive match against the entry's `filename`, e.g. `application_draft.pdf` or `evidence/E-01.jpg`). 404 for another org's case, no built package, an unknown version, or a name not in that version's `files`; 401 when signed out; 500 if the member's SHA-256 differs from its recorded hash. Headers: `Content-Type` (guessed from the filename, fallback `application/octet-stream`), `Content-Disposition: attachment; filename="<last path segment>"`, `Content-Length`, `Cache-Control: private, no-store`, and `X-File-Sha256` (the entry's SHA-256). Not logged (CONTRACT_CHANGES #91). |
| GET | `/cases/{case_id}/case-brief` | PDF (`application/pdf`) | The caseworker's case brief, built on request: summary, timeline, evidence index, gaps. 404 for another org's case, 409 `{ detail: { reasons } }` until an analysis has completed. Starts with a warning when a step of the latest run is `incomplete`. Not stored. |

### Answering a paused run

When the agent needs something only a person knows, a step becomes `needs_input`, the run's `status` becomes `needs_review`, and the step's `summary` holds the question for the caseworker. Nothing else runs until `POST /runs/{run_id}/answer`.

- **Who and where.** The run comes from the path and must belong to the signed-in caseworker's org (404 otherwise, the same answer as an unknown id). The answer is trimmed; empty or over 500 characters is a 422 with the usual `{ detail: { reasons } }`.
- **One answer per pause.** The first answer moves the run to `running` and the step resumes. A second answer for the same pause, or an answer to a run that is not `needs_input`, is a 409.
- **Live model connection.** Once [PR #155](https://github.com/opoeci6/TenantProof/pull/155) ships, answering a paused run returns 409 if the live provider, model, endpoint, or effort no longer matches the saved connection, or if the legacy checkpoint has no saved connection. Answering also returns 409 if `PRIVACY_PLACEHOLDERS` was switched on or off while the run was paused (runs saved before the setting was recorded skip this check, #163). The answer leaves the run and case unchanged. Start a new analysis to use the current setting or connection.
- **A question can name a field.** The agent's question may carry a field key. Only `hearing_date` is defined. The key is not a field of `Run`; the server keeps it with the paused step. It still shows in the step's `tool_calls`, as part of the `ask_caseworker` call's input, like any tool input.
  - For `hearing_date`, the server parses the answer into a date: `YYYY-MM-DD`, or an English month name with day and year (`Nov 21, 2026`, `21 November 2026`). It must be a real calendar date from 2020-01-01 to 2099-12-31. It is stored as `Case.hearing_date` (ISO date) and logged in the activity log. The agent is told the date in the stored form.
  - `none` (also `No hearing yet` or `No hearing scheduled yet`, any case) means no hearing is scheduled yet. The server stores no date, clears `Case.hearing_date` and `Case.next_deadline`, keeps the case out of `hearing_scheduled`, logs "Set the hearing as not scheduled yet (no evidence deadline)", and remembers it (`no_hearing_yet`, not in any response), so a later run does not ask again. The agent is told no hearing is scheduled. A later date answer replaces it. The UI offers this as a **No hearing scheduled yet** button beside the date field. The case brief says "Hearing date: not scheduled yet", the caseload row shows "No hearing yet", and no "Must be received by" date exists.
  - An answer that does not parse is a 422 with a plain reason (`Enter the date like 2026-11-21 or Nov 21, 2026`). The run stays paused and the same question can be answered again.
  - For any other key, or none, the answer is kept as text and goes only to the agent. It is not written to the case.
- **What setting the date changes.** Besides `Case.hearing_date`, the server sets `Case.stage` to `hearing_scheduled` and `Case.next_deadline` to `{ date: the hearing date minus 5 days, label: "Must be received by · hearing <hearing date>" }` (evidence must be *received* at least 5 days before the hearing, Evidence Policy #42 and Regulations s. 25A(2); the date is a receipt date, not a send-by date: registered mail, express post or courier counts as received on the 3rd day after it is sent and e-mail sent at or after 4 p.m. the next day, s. 25A(3B), (3C); the label uses the ISO date like the seeded cases; the label uses the ISO date like the seeded cases). The caseload priority then follows the existing rules. The activity entry names both dates. An answer to a question with any other key changes nothing on the case.

### Personal details and the AI model

With the setting `PRIVACY_PLACEHOLDERS=true` (default false), every model call for a case (the analysis steps, the request translation and the quote translation) gets placeholders such as `[TENANT_NAME]`, `[RENTAL_ADDRESS]` or `[PHONE_1]` instead of the case's personal details, and the real values are put back into what the model returns before any tool, check or person sees it. Quotes are checked against the real evidence text as before; saved events, gaps, form fields and requests hold the real values.

- **What is hidden:** the tenant's name (in full, and the first and last name on their own) and the rental unit's street address from the case; the names, street addresses, phones and emails in the Form J draft; email addresses, phone numbers, postal codes, street addresses, and SIN and card numbers (with a valid check digit) found in the evidence; and whatever the caseworker adds. A company landlord's name, the city and province, dates and amounts stay as written.
- **Placeholders:** each distinct spelling has its own placeholder, so swapping back is exact. Spellings of one person share a stem (`[TENANT_NAME]`, `[TENANT_NAME_2]`). The case's own spelling gets the plain placeholder; found and added values are numbered in exhibit order, so a run and the Evidence page give the same numbers.
- **Not complete:** detection is patterns and known values, not name recognition. The Evidence page shows the list so the caseworker can add what was missed (a name in another script, a neighbour) or send a value as written.

### Tenant intake (no account; access by link token)

| Method | Path | Returns | Notes |
|---|---|---|---|
| GET | `/intake/{token}` | `IntakeView` | Only what the tenant needs: org name, first name, unit, language, open requests. |
| POST | `/intake/{token}/files` | `{ received: number }` | `multipart/form-data`, field `file` (repeatable). |
| POST | `/intake/{token}/submit` | `{ ok: true }` | Body: `{ attest_genuine: true, consent_share: true, language, answers? }`. `answers` is at most 10 items of `{ request_id, text }` (text 1–1000 characters), each for an open `waiting_tenant` request on this case; a bad request id is 422 `{ detail: { reasons } }` and nothing is saved. Each answer is stored as a tenant evidence item (TXT, kind `messages`, locators `message 1`…) and recorded on the gap as `answer`; it never fills a form field by itself (CONTRACT_CHANGES #95). |

A tenant answer is text, not a file: the server makes a TXT evidence item of it, so the agent cites it like a message line and the caseworker can check it. It rides on the same submit as the files, is covered by the genuine-and-consent confirmation, and shares the submit endpoint's rate limit. The answer is never written into a form field; confirming or copying it stays the caseworker's decision. The caseworker sees it on the gap card with a **Show in English** button (the excerpt translation below), labelled a machine translation.

### Reading aid: translating a quoted source

`POST /cases/{case_id}/translate-excerpt` is for the caseworker only. The quote is treated as third-party text: it goes to the model inside an evidence block, never as instructions, and the reply is only shown next to the quote with the label "Machine translation, not the evidence". Quote checks, citations, the form draft and the filing package keep using the original text only. In mock mode the UI says translation is not available.

---

## Shapes

```ts
type User = { id: string; name: string; role: "caseworker"; org: { id: string; name: string } };

type CaseSummary = {
  id: string;
  applicant_role: "tenant" | "landlord";
  tenant_name: string;
  title: string;
  unit: string;
  dispute_type: "maintenance";
  stage: "waiting_tenant" | "needs_review" | "ready_to_file" | "hearing_scheduled";
  next_deadline: { date: string; label: string } | null;   // e.g. "Must be received by (hearing Nov 12, 5-day rule)"
  open_blocking: number;
  pending_review: number;
  tenant_language: string;                                 // "zh", "fr", "en", ...
  priority: "high" | "normal";
  priority_reason: string;                                 // rule that set it, shown to the caseworker
  updated_at: string;
  has_demo_data?: boolean;                                 // prototype only
  analysed?: boolean;   // true once a run has completed or an event has been accepted or edited; false means nothing to file yet
};

type Case = {
  id: string;
  applicant_role: "tenant" | "landlord";
  dispute_type: "maintenance";
  title: string;
  unit: string;
  assigned_to: { id: string; name: string };
  tenant: { name: string; preferred_language: string };
  intake: Intake;
  created_at: string;
  synthetic: boolean;
  has_run: boolean;                      // the case has a run, so GET /runs/latest has one to return (#218)
  has_package: boolean;                  // the case has a built package, so GET /package has one (#218)
};

type Intake = {
  url: string;                           // https://…/i/{token}
  status: "not_sent" | "sent" | "files_received";
  sent_at: string | null;
  last_upload_at: string | null;
  tenant_attested_genuine: boolean;
  tenant_attested_at: string | null;
  qr_svg: string | null;                 // QR code of url, a data URI starting "data:image/svg+xml" (then ";charset=utf-8,…"); POST /cases/{id}/intake-link only, null elsewhere
};

type IntakeView = {
  org_name: string;
  tenant_first_name: string;
  unit: string;
  language: string;
  requests: { id: string; message: string; message_en: string }[];   // approved requests: in the tenant's language, and in English
};

type EvidenceItem = {
  id: string; label: string; filename: string;
  kind: "lease" | "photo" | "messages" | "receipt" | "letter" | "other";
  mime: string; size_bytes: number;
  sha256: string;                        // computed by server at upload
  received_at: string;                   // server clock
  uploaded_by: "tenant" | "caseworker";
  format_check: { accepted: boolean; action: "none" | "converted" | "will_export_pdf" | "rejected"; note: string };
  metadata: { captured_at: string | null; gps_present: boolean; device: string | null; software: string | null };
  metadata_stripped_for_ai: boolean;
  processing: "queued" | "processing" | "done" | "failed";
  error: string | null;
  has_preview: boolean;                  // a copy without EXIF/GPS is at .../evidence/{id}/preview (#97)
};

type RunStep = {
  key: "ingest" | "extract" | "timeline" | "rules" | "gaps" | "draft";
  label: string;
  status: "pending" | "running" | "done" | "incomplete" | "failed" | "needs_input";
  summary: string | null;               // for `needs_input`: the question for the caseworker; for `incomplete`: why it stopped early
  tool_calls: { tool: string; input: string; output: string }[];
};
type Run = { id: string; case_id: string; status: "running" | "needs_review" | "done" | "failed";
             started_at: string; finished_at: string | null; steps: RunStep[] };

type SourceRef = { evidence_id: string; locator: string; excerpt: string | null };
type EventDate = { value: string | null; precision: "day" | "month" | "unknown";
                   basis: "exif" | "message_timestamp" | "document_text" | "filename" | "user" };

type TimelineEvent = {
  id: string; date: EventDate; title: string; description: string;
  actor: "tenant" | "landlord" | "other";
  sources: SourceRef[];
  strength: "corroborated" | "single_source" | "needs_support";
  strength_reasons: string[];
  flags: { type: "no_metadata" | "edited_software" | "date_inferred" | "date_mismatch" | "conflict"; message: string }[];
  review_status: "pending" | "accepted" | "edited" | "rejected";
  caseworker_note: string | null;        // internal, never shown to the tenant or put in the form
  reviewed_by: string | null;
};

type Gap = {
  id: string;
  severity: "blocking" | "recommended";
  title: string; why: string; how_to_fix: string;
  source_id: string | null;
  input: null | { type: "text"; field_key: string; placeholder: string }
               | { type: "choice"; field_key: string; options: string[] };
  tenant_request: null | {               // drafted by the agent, sent only after caseworker approval
    language: string;
    message: string;                     // in the tenant's language
    message_en: string;                  // English for the caseworker
  };
  answer: null | {                       // the tenant typed this on the intake page (CONTRACT_CHANGES #95)
    text: string;
    received_at: string;
    evidence_id: string;                 // the evidence item the answer was stored as
  };
  status: "open" | "waiting_tenant" | "resolved" | "dismissed";
  resolution_note: string | null;
};

type LegalSource = { id: string; title: string; kind: "statute" | "policy" | "form"; url: string | null;
                     last_verified_at: string | null; verify_status: "unchanged" | "changed" | "unreachable" | "unverified";
                     used_for: string };

type FormField = { key: string; label: string; value: string | null; required: boolean;
                   status: "auto_filled" | "needs_confirmation" | "missing" | "user_entered";
                   sources: SourceRef[]; note?: string };
type FormDraft = { form_name: string; disclaimer: string; fields: FormField[];
                   narrative: { text: string; event_ids: string[] } };

type ActivityEntry = { at: string; actor: "agent" | "tenant" | "caseworker"; actor_name: string; action: string };

type Attestations = {
  reviewed_fields: boolean;              // caseworker reviewed every field and the summary
  remedy_confirmed_with_tenant: boolean;
  explained_not_legal_advice: boolean;   // tenant told the agent prepares information, not decisions
};                                       // tenant_attested_genuine comes from Intake, not from this body

type Package = { built_at: string; prepared_by: string;
                 files: { label: string; filename: string; sha256: string; note: string }[];
                 reminders: string[] };
```

## Form J draft fields

The F1–F3 behavior below ships with [PR #143](https://github.com/opoeci6/TenantProof/pull/143). Until then, the two existing structured fields, `monthly_rent` and `tenancy_start`, use the existing amount and date validation below.

- `FormDraft` and `FormField` keep their shapes. The standard prepared tenant draft returns 74 field entries: the seven existing fields and 67 optional, caseworker-entered F1–F3 fields. A case with no stored form fields still returns an empty list. Projected fields have `value: null`, `required: false`, `status: "missing"` and no sources. Known optional keys can be PATCHed even before their rows exist. Unknown keys and another organisation's cases return 404.
- `weekly_rent` and `move_in_date` cover the existing `monthly_rent` and `tenancy_start` requirements, respectively. While an alternative has a value, the legacy field's `required` is false in form responses. Filling the alternative resolves an open or waiting gap for the legacy field. Clearing it reopens a gap resolved by that alternative if the legacy field is still empty. A direct attempt to reopen a covered resolved gap returns 422 with `detail: "Clear the corresponding Form J field before reopening this item"`. The stage, package checks and deterministic missing-field checks follow the same requirements.
- A new value is trimmed and checked as follows. Business rule failures return 422 with `{ detail: string }`; the case and form are left unchanged. An empty form PATCH value can clear a field. Sending back the stored value bypasses format checks so a legacy verbatim draft can still be confirmed. The rent-period conflict check still applies to any nonempty rent write.

| Fields | Accepted nonempty value |
|---|---|
| `tenant_email`, `landlord_email` | An email address in the server's accepted syntax |
| `monthly_rent`, `weekly_rent`, `deposit_amount` | Positive CAD amount with up to two decimal places. Grouped thousands, optional `$` and an optional `CAD` prefix or suffix are accepted. A monthly and a weekly rent cannot both be set. Clear the existing rent before saving the other period. |
| `tenancy_start` | A calendar date in `YYYY-MM-DD`, year 1900 through the current year |
| `move_in_date`, `move_out_date`, `fixed_term_start`, `fixed_term_end` | A calendar date in `YYYY-MM-DD`, year 1900 or later. When both dates of a pair are set, `move_out_date` cannot be earlier than `move_in_date` and `fixed_term_end` cannot be earlier than `fixed_term_start`; equal dates, blank counterparts, and confirming an unchanged stored value are allowed. A contradictory edit in either direction returns 422 `{ detail: string }` and leaves the saved value unchanged (#196). |
| `notice_given_date`, `notice_effective_date`, `deposit_paid_date` | A calendar date in `YYYY-MM-DD`, year 1900 or later. Notice dates are not ordered on the server, so a notice that appears backdated can be transcribed as received; the UI shows a non-blocking warning when the effective date is earlier than the date notice was given (#196). |
| `applicant_email_consent`, `notice_given`, `written_lease`, `lease_copy_provided`, `act_copy_provided`, `deposit_paid`, `deposit_in_trust` | `yes` or `no` |
| `notice_given_by` | `landlord` or `tenant` |
| `lease_term` | `year_to_year`, `month_to_month`, `week_to_week` or `fixed_term` |
| `request_termination`, `request_compliance`, `request_repairs`, `request_set_aside_notice`, `request_payment`, `request_rent_relief`, `request_expense_compensation`, `request_application_fee`, `request_other` | `yes` to select. Send an empty value to clear. Each key has a separate text field with the suffix `_reason`. |

---

## Priority rules (caseload)

Rule-based so the caseworker can see why a case is on top:

1. `high` if an evidence deadline is within 7 days or has passed. Evidence must be received 5 days before the hearing (Evidence Policy #42; registered mail, express post or courier counts as received on the 3rd day after it is sent). The reason reads "Must be received in N days" ("in 1 day", "today"), or "Deadline passed (N days ago)" once the date is behind us; counts are pluralised ("1 required item", "2 required items").
2. `high` if a blocking gap is open and a hearing is scheduled.
3. Otherwise `normal`. Sort: priority, then nearest deadline, then most blocking items.

---

## Who owns what (suggested)

| Endpoint group | Module |
|---|---|
| `/me`, `/cases`, `/activity`, auth, intake token | Backend skeleton (tech lead) |
| `/evidence`, `/intake/{token}/files` | Evidence extraction |
| `/runs` (steps + tool_calls log), `tenant_request` drafting | Agent orchestration |
| `/timeline` | Evidence extraction + agent |
| `/sources`, `rules` step | RAG |
| `/gaps`, `/form-draft` | Form generation |
| `/package` | Frontend + export |

Things to verify before the demo: the official form name and fields, the statute section used for the
repair obligation, and every URL in `LegalSource`. The mock marks unverified ones as `unverified`.
