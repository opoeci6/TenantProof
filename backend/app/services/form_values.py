"""Validation and display of structured form field values."""

import re
from datetime import date
from decimal import Decimal

MIN_TENANCY_YEAR = 1900

_RENT = re.compile(
    r"(?P<prefix>CAD[ \t]+)?\$?[ \t]*"
    r"(?P<amount>(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?)"
    r"(?P<suffix>[ \t]+CAD)?",
    re.IGNORECASE,
)


def monthly_rent_amount(value: str) -> Decimal | None:
    """Read a positive Canadian dollar amount without rounding or assuming a foreign currency."""
    match = _RENT.fullmatch(value.strip())
    if match is None or (match["prefix"] and match["suffix"]):
        return None
    amount = Decimal(match["amount"].replace(",", ""))
    return amount if amount > 0 else None


def is_tenancy_start_date(value: str, current_year: int) -> bool:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value and MIN_TENANCY_YEAR <= parsed.year <= current_year
