"""Validation and display of structured form field values."""

import re
from datetime import date
from decimal import Decimal

MIN_TENANCY_YEAR = 1900

F2_DATE_KEYS = frozenset({
    "move_in_date", "move_out_date", "notice_given_date", "notice_effective_date",
    "fixed_term_start", "fixed_term_end", "deposit_paid_date",
})
MONEY_KEYS = frozenset({"monthly_rent", "weekly_rent", "deposit_amount"})
RENT_KEYS = frozenset({"monthly_rent", "weekly_rent"})
EMAIL_KEYS = frozenset({"tenant_email", "landlord_email"})
_YES_NO = {"yes": "Yes", "no": "No"}
F1_CHOICES = {"applicant_email_consent": _YES_NO}
F2_CHOICES = {
    "notice_given": _YES_NO,
    "notice_given_by": {"landlord": "Landlord", "tenant": "Tenant"},
    "written_lease": _YES_NO,
    "lease_term": {
        "year_to_year": "Year to year", "month_to_month": "Month to month",
        "week_to_week": "Week to week", "fixed_term": "Fixed term",
    },
    "lease_copy_provided": _YES_NO,
    "act_copy_provided": _YES_NO,
    "deposit_paid": _YES_NO,
    "deposit_in_trust": _YES_NO,
}
FORM_CHOICES = {**F1_CHOICES, **F2_CHOICES}

# A blank answer is unknown. Hide related values only when explicitly inapplicable.
F2_DEPENDENCIES = {
    "notice_given_by": ("notice_given", "yes"),
    "notice_given_date": ("notice_given", "yes"),
    "notice_effective_date": ("notice_given", "yes"),
    "fixed_term_start": ("lease_term", "fixed_term"),
    "fixed_term_end": ("lease_term", "fixed_term"),
    "deposit_amount": ("deposit_paid", "yes"),
    "deposit_paid_date": ("deposit_paid", "yes"),
    "deposit_in_trust": ("deposit_paid", "yes"),
}

_RENT = re.compile(
    r"(?P<prefix>CAD[ \t]+)?\$?[ \t]*"
    r"(?P<amount>(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?)"
    r"(?P<suffix>[ \t]+CAD)?",
    re.IGNORECASE,
)
_DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL = re.compile(
    rf"[A-Za-z0-9.!#$%&'*+/=?^_`{{|}}~-]+@{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})*"
)


def cad_amount(value: str) -> Decimal | None:
    """Read a positive Canadian dollar amount without rounding or assuming a foreign currency."""
    match = _RENT.fullmatch(value.strip())
    if match is None or (match["prefix"] and match["suffix"]):
        return None
    amount = Decimal(match["amount"].replace(",", ""))
    return amount if amount > 0 else None


def is_email_address(value: str) -> bool:
    return _EMAIL.fullmatch(value) is not None


def is_tenancy_start_date(value: str, max_year: int = date.max.year) -> bool:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value and MIN_TENANCY_YEAR <= parsed.year <= max_year
