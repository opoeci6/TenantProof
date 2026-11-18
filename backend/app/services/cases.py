"""Creating a case (POST /cases, issue #41, CONTRACT_CHANGES #35).

A new case starts where the live-demo case starts: no evidence, no run, the intake link not sent
yet. The org and the assignee are the signed-in caseworker's; nothing in the body can choose them.
"""

import unicodedata
import uuid

from sqlmodel import Session

from app.models import CaseRow, UserRow
from app.schemas import CaseCreate
from app.services import activity
from app.services.agent.tools import clean_text
from app.services.legal.corpus import FORM_J_DRAFT_LABEL

FORM_NAME = FORM_J_DRAFT_LABEL
FORM_DISCLAIMER = (
    "Prepared with the tenant to help fill in the official form. It is information, not legal "
    "advice, and is not submitted anywhere."
)
LIMITS = {"tenant_name": 120, "unit": 200, "title": 120}  # the CaseCreate limits
LABELS = {
    "tenant_name": "Tenant name",
    "tenant_language": "Preferred language",
    "unit": "Unit address",
    "title": "What is wrong",
}
# "<" and its look-alikes are refused, not escaped: escaped, a typed "Heat < 15C" would be stored
# and shown (caseload, tenant page, filed PDF) as "Heat &lt; 15C".
ANGLES = ("<", "＜", "﹤")


def clean_typed(value: str, limit: int) -> str:
    """No control characters (`clean_text`) and no invisible format characters (zero-width
    spaces, the byte-order mark, bidi overrides that reverse how text displays)."""
    visible = "".join(ch for ch in value if unicodedata.category(ch) != "Cf")
    return clean_text(visible, limit)


def cleaned(body: CaseCreate) -> dict[str, str]:
    """The typed text, cleaned like any text a model reads later (the title and the tenant name go
    into the agent's prompts and the filing package). With "<" refused first (`problems`), the
    cleaning only removes characters, so nothing grows past its limit."""
    return {key: clean_typed(getattr(body, key), limit) for key, limit in LIMITS.items()}


def blank(value: str) -> bool:
    return not any(ch.isprintable() and not ch.isspace() for ch in value)


def problems(body: CaseCreate) -> list[str]:
    """Why a body that passed CaseCreate still cannot be saved, one plain reason per field: a "<",
    or nothing visible left once control and format characters are removed."""
    text = cleaned(body)
    reasons: list[str] = []
    for key in LIMITS:
        if any(angle in getattr(body, key) for angle in ANGLES):
            reasons.append(f"{LABELS[key]}: remove the < character")
        elif blank(text[key]):
            reasons.append(f"{LABELS[key]}: enter some text")
    return reasons


def create_case(session: Session, user: UserRow, body: CaseCreate) -> CaseRow:
    """The new case, in the user's org and assigned to them. Caller checks `problems` first, and
    commits."""
    text = cleaned(body)
    case = CaseRow(
        id=f"case_{uuid.uuid4().hex[:10]}",
        org_id=user.org_id,
        assigned_to_id=user.id,
        dispute_type="maintenance",
        title=text["title"],
        unit=text["unit"],
        tenant_name=text["tenant_name"],
        tenant_language=body.tenant_language,
        stage="waiting_tenant",
        synthetic=True,  # every case in this prototype is synthetic demo data
        has_demo_data=True,  # it has a case view: the caseload opens it like the live-demo case
        form_name=FORM_NAME,
        form_disclaimer=FORM_DISCLAIMER,
        intake_status="not_sent",
    )
    session.add(case)
    session.flush()  # the activity row's foreign key needs the case row first
    activity.log(session, case.id, "caseworker", user.name, "Created the case")
    return case
