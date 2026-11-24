"""Additional Form J fields that the caseworker completes directly."""

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, select

from app.models import CaseRow, FormFieldRow
from app.services.legal.corpus import FORM_J_FIELDS, FormFieldSpec

F2_FIELDS = (
    FormFieldSpec("move_in_date", "Move-in date", required=False),
    FormFieldSpec("move_out_date", "Move-out date", required=False),
    FormFieldSpec("notice_given", "Notice to quit given", required=False),
    FormFieldSpec("notice_given_by", "Notice given by", required=False),
    FormFieldSpec("notice_given_date", "Date notice was given", required=False),
    FormFieldSpec("notice_effective_date", "Effective date of notice", required=False),
    FormFieldSpec("written_lease", "Written lease", required=False),
    FormFieldSpec("lease_term", "Lease term", required=False),
    FormFieldSpec("fixed_term_start", "Fixed-term lease start date", required=False),
    FormFieldSpec("fixed_term_end", "Fixed-term lease end date", required=False),
    FormFieldSpec("weekly_rent", "Weekly rent", required=False),
    FormFieldSpec("lease_copy_provided", "Copy of the lease provided", required=False),
    FormFieldSpec(
        "act_copy_provided", "Copy of the Residential Tenancies Act provided", required=False,
    ),
    FormFieldSpec("deposit_paid", "Security deposit paid", required=False),
    FormFieldSpec("deposit_amount", "Security deposit amount", required=False),
    FormFieldSpec("deposit_paid_date", "Security deposit payment date", required=False),
    FormFieldSpec("deposit_in_trust", "Security deposit held in trust", required=False),
)
F2_BY_KEY = {field.key: field for field in F2_FIELDS}
_F2_POSITIONS = {field.key: i for i, field in enumerate(F2_FIELDS, start=len(FORM_J_FIELDS))}
F2_ALTERNATIVES = {"weekly_rent": "monthly_rent", "move_in_date": "tenancy_start"}
LEGACY_ALTERNATIVES = {legacy: current for current, legacy in F2_ALTERNATIVES.items()}


def _optional_field(case_id: str, spec: FormFieldSpec) -> FormFieldRow:
    return FormFieldRow(
        case_id=case_id, key=spec.key, position=_F2_POSITIONS[spec.key],
        label=spec.label, required=spec.required, status="missing",
    )


def with_optional_fields(case: CaseRow, fields: Sequence[FormFieldRow]) -> list[FormFieldRow]:
    existing = {field.key: field for field in fields}
    return [
        *(field for field in fields if field.key not in F2_BY_KEY),
        *(existing[spec.key] if spec.key in existing else _optional_field(case.id, spec)
          for spec in F2_FIELDS),
    ]


def materialize_optional_field(session: Session, case_id: str, key: str) -> FormFieldRow | None:
    spec = F2_BY_KEY.get(key)
    if spec is None:
        return None
    field = _optional_field(case_id, spec)
    stmt = insert(FormFieldRow).values(**field.model_dump()).on_conflict_do_nothing(
        index_elements=["case_id", "key"]
    )
    session.exec(stmt)
    return session.get(FormFieldRow, (case_id, key))


def covered_legacy_fields(fields: Sequence[FormFieldRow]) -> set[str]:
    return {F2_ALTERNATIVES[f.key] for f in fields if f.key in F2_ALTERNATIVES and f.value}


def covered_legacy_field_for_case(session: Session, case_id: str, key: str) -> bool:
    alternative = LEGACY_ALTERNATIVES.get(key)
    if alternative is None:
        return False
    field = session.get(FormFieldRow, (case_id, alternative))
    return field is not None and bool(field.value)


def is_required(field: FormFieldRow, covered_fields: set[str]) -> bool:
    return field.required and field.key not in covered_fields


def lock_alternative_fields(session: Session, case_id: str) -> None:
    # Serialize changes to alternative fields and their shared gaps.
    session.exec(select(CaseRow.id).where(CaseRow.id == case_id).with_for_update()).one()
