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
F1_ADDRESS_GROUPS = {
    "tenant_civic": "Tenant civic address",
    "tenant_mailing": "Tenant mailing address",
    "landlord_civic": "Landlord civic address",
    "landlord_mailing": "Landlord mailing address",
    "premises": "Rental premises address",
}
F1_ADDRESS_PARTS = {
    "street": "Street address",
    "apartment": "Apartment or unit",
    "city": "City or town",
    "province": "Province or territory",
    "postal_code": "Postal code",
}
F1_CONTACT_GROUPS = {
    "Tenant contact details": {
        "tenant_home_phone": "Home phone",
        "tenant_other_phone": "Business or other phone",
        "tenant_email": "Email",
        "applicant_email_consent": "Consent to email from Service Nova Scotia",
    },
    "Landlord contact details": {
        "landlord_home_phone": "Home phone",
        "landlord_other_phone": "Business or other phone",
        "landlord_email": "Email",
    },
}
F1_FIELDS = (
    *(FormFieldSpec(f"{prefix}_{part}", f"{group} · {label}", required=False)
      for prefix, group in F1_ADDRESS_GROUPS.items() for part, label in F1_ADDRESS_PARTS.items()),
    *(FormFieldSpec(key, f"{group} · {label}", required=False)
      for group, fields in F1_CONTACT_GROUPS.items() for key, label in fields.items()),
)
F2_FIELD_KEYS = frozenset(field.key for field in F2_FIELDS)
F3_REQUESTS = {
    "request_termination": "Termination of tenancy",
    "request_compliance": "Compliance with a lease or the Act",
    "request_repairs": "Repairs",
    "request_set_aside_notice": "Set aside a notice to quit given by the landlord",
    "request_payment": "Payment of money",
    "request_rent_relief": "Relief from rent owing",
    "request_expense_compensation": "Compensation for expenses",
    "request_application_fee": "Award of the application fee",
    "request_other": "Other",
}
F3_PAYMENT_REQUESTS = frozenset({"request_rent_relief", "request_expense_compensation"})
F3_FIELDS = tuple(
    field
    for key, label in F3_REQUESTS.items()
    for field in (
        FormFieldSpec(key, label, required=False),
        FormFieldSpec(f"{key}_reason", f"Reason for {label.lower()}", required=False),
    )
)
MANUAL_FIELDS = (*F2_FIELDS, *F1_FIELDS, *F3_FIELDS)
MANUAL_FIELDS_BY_KEY = {field.key: field for field in MANUAL_FIELDS}
_MANUAL_POSITIONS = {
    field.key: i for i, field in enumerate(MANUAL_FIELDS, start=len(FORM_J_FIELDS))
}
F2_ALTERNATIVES = {"weekly_rent": "monthly_rent", "move_in_date": "tenancy_start"}
LEGACY_ALTERNATIVES = {legacy: current for current, legacy in F2_ALTERNATIVES.items()}


def _optional_field(case_id: str, spec: FormFieldSpec) -> FormFieldRow:
    return FormFieldRow(
        case_id=case_id, key=spec.key, position=_MANUAL_POSITIONS[spec.key],
        label=spec.label, required=spec.required, status="missing",
    )


def with_optional_fields(case: CaseRow, fields: Sequence[FormFieldRow]) -> list[FormFieldRow]:
    existing = {field.key: field for field in fields}
    return [
        *(field for field in fields if field.key not in MANUAL_FIELDS_BY_KEY),
        *(existing[spec.key] if spec.key in existing else _optional_field(case.id, spec)
          for spec in MANUAL_FIELDS),
    ]


def materialize_optional_field(session: Session, case_id: str, key: str) -> FormFieldRow | None:
    spec = MANUAL_FIELDS_BY_KEY.get(key)
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
