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
| GET | `/me` | `User` | Signed-in caseworker. MVP can hard-code one user. |
| GET | `/cases` | `CaseSummary[]` | Caseload, already sorted by priority (rule-based). |
| GET | `/cases/{case_id}` | `Case` | |
| POST | `/cases/{case_id}/intake-link` | `Intake` | Creates or re-sends the tenant link. |
| GET | `/cases/{case_id}/evidence` | `EvidenceItem[]` | |
| POST | `/cases/{case_id}/evidence` | `EvidenceItem` | Caseworker upload, `multipart/form-data`, field `file`. |
| POST | `/cases/{case_id}/runs` | `Run` | Starts analysis, returns immediately with `status: "running"`. |
| GET | `/cases/{case_id}/runs/latest` | `Run` | 404 if none. |
| GET | `/runs/{run_id}` | `Run` | UI polls every ~1 s until `status` is not `running`. |
| GET | `/cases/{case_id}/timeline` | `TimelineEvent[]` | Sorted by date. |
| PATCH | `/timeline/{event_id}` | `TimelineEvent` | Body: `{ review_status, title?, description?, date?, caseworker_note? }` |
| GET | `/cases/{case_id}/gaps` | `Gap[]` | |
| PATCH | `/gaps/{gap_id}` | `Gap` | Body: `{ status, resolution_note?, value? }` |
| POST | `/gaps/{gap_id}/tenant-request` | `Gap` | Caseworker approved the drafted message. Sends it via the intake link; gap → `waiting_tenant`. |
| GET | `/cases/{case_id}/sources` | `LegalSource[]` | |
| GET | `/cases/{case_id}/form-draft` | `FormDraft` | Narrative rebuilt from accepted events on every GET. |
| PATCH | `/form-draft/fields/{key}` | `FormField` | Body: `{ value }` → `user_entered`. |
| GET | `/cases/{case_id}/activity` | `ActivityEntry[]` | Newest first. |
| POST | `/cases/{case_id}/package` | `Package` | Body: `{ attestations }`. 409 if blocking gaps open or tenant hasn't attested. |

### Tenant intake (no account; access by link token)

| Method | Path | Returns | Notes |
|---|---|---|---|
| GET | `/intake/{token}` | `IntakeView` | Only what the tenant needs: org name, first name, unit, language, open requests. |
| POST | `/intake/{token}/files` | `{ received: number }` | `multipart/form-data`, field `file` (repeatable). |
| POST | `/intake/{token}/submit` | `{ ok: true }` | Body: `{ attest_genuine: true, consent_share: true, language }` |

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
  next_deadline: { date: string; label: string } | null;   // e.g. "Evidence due (hearing Nov 12, 5-day rule)"
  open_blocking: number;
  pending_review: number;
  tenant_language: string;                                 // "zh", "fr", "en", ...
  priority: "high" | "normal";
  priority_reason: string;                                 // rule that set it, shown to the caseworker
  updated_at: string;
  has_demo_data?: boolean;                                 // prototype only
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
};

type IntakeView = {
  org_name: string;
  tenant_first_name: string;
  unit: string;
  language: string;
  requests: { id: string; message: string }[];   // approved requests, in the tenant's language
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
};

type RunStep = {
  key: "ingest" | "extract" | "timeline" | "rules" | "gaps" | "draft";
  label: string;
  status: "pending" | "running" | "done" | "failed" | "needs_input";
  summary: string | null;
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
  flags: { type: "no_metadata" | "edited_software" | "date_inferred" | "date_mismatch"; message: string }[];
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

1. `high` if an evidence deadline is within 7 days. Evidence is due 5 days before the hearing (Evidence Policy #42).
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
