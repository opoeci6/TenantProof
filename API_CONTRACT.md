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
4. **Long work is a job.** Analysis is a `Run` with six steps. The UI polls; steps can be `failed` or `needs_input`.
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

| Method | Path | Returns | Notes |
|---|---|---|---|
| POST | `/auth/login` | `User` | Body: `{ email, password }`. Sets the session cookie (httpOnly, SameSite=Lax). 401 `Wrong email or password` for an unknown email and a wrong password alike; 429 when rate limited. |
| POST | `/auth/logout` | `{ ok }` | Ends the session and clears the cookie. |
| GET | `/me` | `User` | Signed-in caseworker (401 when signed out). |
| GET | `/cases` | `CaseSummary[]` | Caseload, already sorted by priority (rule-based). |
| POST | `/cases` | `Case` | Creates a case in the signed-in caseworker's org, assigned to them. Body: `CaseCreate` `{ tenant_name, tenant_language, unit, title }`; `tenant_language` is one of `en`, `zh`, `fr`, `es`, `ru`, `hi`. 422 `{ detail: { reasons } }` for a blank, too long or unknown value, or an extra field. See CONTRACT_CHANGES #35. |
| GET | `/cases/{case_id}` | `Case` | |
| POST | `/cases/{case_id}/intake-link` | `Intake` | Creates or re-sends the tenant link. |
| GET | `/cases/{case_id}/evidence` | `EvidenceItem[]` | |
| POST | `/cases/{case_id}/evidence` | `EvidenceItem` | Caseworker upload, `multipart/form-data`, field `file`. |
| GET | `/cases/{case_id}/evidence/{evidence_id}/preview` | image (`image/jpeg` or `image/png`) | The photo as the caseworker sees it: the copy made at upload without EXIF or GPS, never the original (a HEIC's copy is a JPEG). `Cache-Control: private, no-cache` with `ETag: "<sha256 of the copy>"`, so the browser checks the session on every view and an unchanged copy answers 304 without its bytes. 404 for another org's case, an item in another case, or an item with `has_preview: false`; 401 when not signed in (CONTRACT_CHANGES #97). |
| POST | `/cases/{case_id}/runs` | `Run` | Starts analysis, returns immediately with `status: "running"`. |
| GET | `/cases/{case_id}/runs/latest` | `Run` | 404 if none. |
| GET | `/runs/{run_id}` | `Run` | UI polls every ~1 s until `status` is not `running`. |
| POST | `/runs/{run_id}/answer` | `Run` | Body: `{ answer }`, a string of 1 to 500 characters. Resumes a run paused by `ask_caseworker` and returns it with `status: "running"`. 404 for an unknown run or another org's run, 409 when the run is not `needs_input` or this pause was already answered, 422 `{ detail: { reasons } }` when the answer is not a valid date for a date question (the run stays paused). See "Answering a paused run". |
| GET | `/cases/{case_id}/timeline` | `TimelineEvent[]` | Sorted by date. |
| PATCH | `/timeline/{event_id}` | `TimelineEvent` | Body: `{ review_status, title?, description?, date?, caseworker_note? }` |
| GET | `/cases/{case_id}/gaps` | `Gap[]` | |
| PATCH | `/gaps/{gap_id}` | `Gap` | Body: `{ status, resolution_note?, value? }` |
| POST | `/gaps/{gap_id}/tenant-request` | `Gap` | Caseworker approved the drafted message. Sends it via the intake link; gap → `waiting_tenant`. Optional body `{ message, message_en }`: the request as the caseworker edited it (CONTRACT_CHANGES #37). |
| POST | `/gaps/{gap_id}/tenant-request/translate` | `{ message }` | Body: `{ message_en }`. Translates the caseworker's English into the tenant's language with one model call. Saves nothing (CONTRACT_CHANGES #37). |
| POST | `/cases/{case_id}/translate-excerpt` | `{ text_en }` | Body: `{ excerpt, language? }` (excerpt 1–3000 characters; language defaults to the case's tenant language). One model call for a quote not seen before; the result is cached by language + excerpt (`excerpt_translation`) and returned without a model call on a repeat. An ASCII-only excerpt is returned unchanged. 404 for another org's case, 503 `{ detail: { reasons } }` when the model is unavailable. A reading aid only: never cited, filed, or used to check a quote (CONTRACT_CHANGES #96). |
| GET | `/cases/{case_id}/sources` | `LegalSource[]` | |
| GET | `/cases/{case_id}/form-draft` | `FormDraft` | Narrative rebuilt from accepted events on every GET. |
| PATCH | `/cases/{case_id}/form-draft/fields/{key}` | `FormField` | Body: `{ value }` → `user_entered`. |
| GET | `/cases/{case_id}/activity` | `ActivityEntry[]` | Newest first. |
| POST | `/cases/{case_id}/package` | `Package` | Body: `{ attestations }`. 409 if blocking gaps open or tenant hasn't attested. |
| GET | `/cases/{case_id}/package/download` | zip (`application/zip`) | Streams a built package. Query `version` (default: the latest); 404 if none is built. Headers: `Content-Disposition: attachment; filename="tenantproof-<case>-v<version>.zip"` and `X-Package-Sha256` (the zip's SHA-256). Images in the zip have their GPS data removed and a HEIC is filed as a JPG (CONTRACT_CHANGES). |
| GET | `/cases/{case_id}/case-brief` | PDF (`application/pdf`) | The caseworker's case brief, built on request: summary, timeline, evidence index, gaps. 404 for another org's case, 409 `{ detail: { reasons } }` until an analysis has completed. Not stored. |

### Answering a paused run

When the agent needs something only a person knows, a step becomes `needs_input`, the run's `status` becomes `needs_review`, and the step's `summary` holds the question for the caseworker. Nothing else runs until `POST /runs/{run_id}/answer`.

- **Who and where.** The run comes from the path and must belong to the signed-in caseworker's org (404 otherwise, the same answer as an unknown id). The answer is trimmed; empty or over 500 characters is a 422 with the usual `{ detail: { reasons } }`.
- **One answer per pause.** The first answer moves the run to `running` and the step resumes. A second answer for the same pause, or an answer to a run that is not `needs_input`, is a 409.
- **A question can name a field.** The agent's question may carry a field key. Only `hearing_date` is defined. The key is not a field of `Run`; the server keeps it with the paused step. It still shows in the step's `tool_calls`, as part of the `ask_caseworker` call's input, like any tool input.
  - For `hearing_date`, the server parses the answer into a date: `YYYY-MM-DD`, or an English month name with day and year (`Nov 21, 2026`, `21 November 2026`). It must be a real calendar date from 2020-01-01 to 2099-12-31. It is stored as `Case.hearing_date` (ISO date) and logged in the activity log. The agent is told the date in the stored form.
  - An answer that does not parse is a 422 with a plain reason (`Enter the date like 2026-11-21 or Nov 21, 2026`). The run stays paused and the same question can be answered again.
  - For any other key, or none, the answer is kept as text and goes only to the agent. It is not written to the case.
- **What setting the date changes.** Besides `Case.hearing_date`, the server sets `Case.stage` to `hearing_scheduled` and `Case.next_deadline` to `{ date: the hearing date minus 5 days, label: "Must be received by · hearing <hearing date>" }` (evidence must be *received* at least 5 days before the hearing, Evidence Policy #42 and Regulations s. 25A(2); the date is a receipt date, not a send-by date: registered mail, express post or courier counts as received on the 3rd day after it is sent and e-mail sent at or after 4 p.m. the next day, s. 25A(3B), (3C); the label uses the ISO date like the seeded cases; the label uses the ISO date like the seeded cases). The caseload priority then follows the existing rules. The activity entry names both dates. An answer to a question with any other key changes nothing on the case.

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
  dispute_type: "maintenance";
  title: string;
  unit: string;
  assigned_to: { id: string; name: string };
  tenant: { name: string; preferred_language: string };
  intake: Intake;
  created_at: string;
  synthetic: boolean;
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
  status: "pending" | "running" | "done" | "failed" | "needs_input";
  summary: string | null;               // for `needs_input`: the question for the caseworker
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
