"""Issue #162: the Form draft's progress count uses the same rule as the page for which fields
are shown (`formFieldApplies` in web/index.html, a pure function run under node). A field hidden by
its parent answer stops counting, in the filled count and in the total, and counts again once it is
shown; hidden values are kept. The rule must also agree with the server's own copy
(`F2_DEPENDENCIES`, the payment request sets), which the draft PDF uses."""

import json
import re
import shutil
from pathlib import Path

import pytest

from app.services.form_fields import (
    F1_FIELDS,
    F2_FIELDS,
    F3_FIELDS,
    F3_PAYMENT_REQUESTS,
    F3_REQUESTS,
    F4_FIELDS,
    F4_PAYMENT_REQUESTS,
    F4_REQUESTS,
)
from app.services.form_values import F2_DEPENDENCIES
from app.services.legal.corpus import FORM_J_FIELDS
from tests.node_support import node_json

INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"
APPLIES = re.compile(r"/\* formApplies:start.*?\*/(.*?)/\* formApplies:end \*/", re.S)
PURE = re.compile(r"/\* pure:start.*?\*/(.*?)/\* pure:end \*/", re.S)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def js(expr: str) -> object:
    html = INDEX.read_text(encoding="utf-8")
    applies, pure = APPLIES.search(html), PURE.search(html)
    assert applies and pure, "formApplies or pure block not found in web/index.html"
    return node_json(f"{applies.group(1)}\n{pure.group(1)}\nconsole.log(JSON.stringify({expr}));")


def draft(landlord: bool, **values: str) -> list[dict]:
    """An empty draft as GET /form-draft returns it for the role (the six or seven required
    Form J fields, all F1 and F2 fields, and the role's own requests), with `values` filled in."""
    required = [s for s in FORM_J_FIELDS if not (landlord and s.key == "remedy")]
    optional = [*F2_FIELDS, *F1_FIELDS, *(F4_FIELDS if landlord else F3_FIELDS)]
    def empty(key: str, label: str, required: bool) -> dict:
        return {"key": key, "label": label, "required": required, "status": "missing",
                "value": None}

    fields = [empty(s.key, s.label, True) for s in required]
    fields += [empty(s.key, s.label, False) for s in optional]
    keys = {f["key"] for f in fields}
    assert set(values) <= keys, f"not on this draft: {set(values) - keys}"
    for f in fields:
        if f["key"] in values:
            f["value"] = values[f["key"]] or None
            f["status"] = "user_entered" if values[f["key"]] else "missing"
    return fields


def counter(landlord: bool, **values: str) -> str:
    """The sidebar's form line ("1 of 7 fields") for that draft."""
    fields = json.dumps(draft(landlord, **values))
    counts = f"formFieldCounts(applicableFormFields({fields}, {json.dumps(landlord)}))"
    expr = f"railMeta({{...{counts}, "
    expr += "gaps: 0, total: 0, reviewed: 0, blocking: 0, recommended: 0, waiting: 0}, 0).form"
    return js(expr)  # type: ignore[return-value]


def shown(landlord: bool, fields: list[dict]) -> list[str]:
    return js(f"applicableFormFields({json.dumps(fields)}, {json.dumps(landlord)})"
              ".map(f => f.key)")  # type: ignore[return-value]


LANDLORD, TENANT = True, False
BOTH_ROLES = pytest.mark.parametrize(("landlord", "base"), [(LANDLORD, 6), (TENANT, 7)],
                                     ids=["landlord", "tenant"])


def line(filled: int, total: int) -> str:
    return f"{filled} of {total} fields"


# --------------------------------------------------------- the issue's table, F2 in both roles


@BOTH_ROLES
def test_notice_no_hides_and_uncounts_who_gave_notice(landlord: bool, base: int) -> None:
    assert counter(landlord) == line(0, base)
    assert counter(landlord, notice_given="yes", notice_given_by="landlord") == line(2, base + 2)
    assert counter(landlord, notice_given="no", notice_given_by="landlord") == line(1, base + 1)


@BOTH_ROLES
def test_deposit_no_hides_and_uncounts_the_amount(landlord: bool, base: int) -> None:
    assert counter(landlord, deposit_paid="yes", deposit_amount="150") == line(2, base + 2)
    assert counter(landlord, deposit_paid="no", deposit_amount="150") == line(1, base + 1)


@BOTH_ROLES
def test_every_dependent_of_a_no_answer_stops_counting(landlord: bool, base: int) -> None:
    values = {"notice_given": "no", "notice_given_by": "tenant", "notice_given_date": "2026-09-01",
              "notice_effective_date": "2026-10-31", "deposit_paid": "no", "deposit_amount": "150",
              "deposit_paid_date": "2026-05-01", "deposit_in_trust": "yes",
              "lease_term": "month_to_month", "fixed_term_start": "2026-05-01",
              "fixed_term_end": "2027-04-30"}
    assert counter(landlord, **values) == line(3, base + 3)


@BOTH_ROLES
def test_answering_yes_again_restores_the_kept_values(landlord: bool, base: int) -> None:
    kept = {"notice_given_by": "landlord", "deposit_amount": "150"}
    assert counter(landlord, notice_given="no", deposit_paid="no", **kept) == line(2, base + 2)
    assert counter(landlord, notice_given="yes", deposit_paid="yes", **kept) == line(4, base + 4)


@BOTH_ROLES
def test_a_blank_parent_answer_hides_nothing(landlord: bool, base: int) -> None:
    # Blank means unknown, not "No" (the page and F2_DEPENDENCIES agree): a filled dependent counts.
    assert counter(landlord, notice_given_by="landlord") == line(1, base + 1)


# ---------------------------------------------------------------------------- F4 payment (landlord)


def test_f4_payment_off_uncounts_its_child_and_reason() -> None:
    values = {"landlord_request_payment": "yes", "landlord_request_unpaid_rent": "yes",
              "landlord_request_unpaid_rent_reason": "October rent not paid"}
    assert counter(LANDLORD, **values) == "3 of 9 fields"
    assert counter(LANDLORD, **{**values, "landlord_request_payment": ""}) == "0 of 6 fields"


def test_f4_payment_off_uncounts_every_child_and_the_payment_reason() -> None:
    values = {"landlord_request_payment": "yes", "landlord_request_payment_reason": "Money owed",
              "landlord_request_unpaid_rent": "yes", "landlord_request_unpaid_rent_reason": "Rent",
              "landlord_request_property_damage": "yes",
              "landlord_request_property_damage_reason": "Window",
              "landlord_request_other_payment": "yes"}
    assert counter(LANDLORD, **values) == "7 of 13 fields"
    off = {**values, "landlord_request_payment": ""}
    assert counter(LANDLORD, **off) == "0 of 6 fields"
    assert counter(LANDLORD, **values) == "7 of 13 fields"  # re-enabled: kept values count again


def test_an_unselected_requests_kept_reason_does_not_count() -> None:
    assert counter(LANDLORD, landlord_request_compliance_reason="Kept") == "0 of 6 fields"
    assert counter(LANDLORD, landlord_request_compliance="yes",
                   landlord_request_compliance_reason="Kept from before") == "2 of 8 fields"


def test_an_unselected_payment_childs_reason_does_not_count() -> None:
    assert counter(LANDLORD, landlord_request_payment="yes",
                   landlord_request_unpaid_rent_reason="Kept") == "1 of 7 fields"


# ------------------------------------------------------------------------------ F3 payment (tenant)


def test_f3_payment_off_uncounts_both_children_and_their_reasons() -> None:
    values = {"request_payment": "yes", "request_rent_relief": "yes",
              "request_rent_relief_reason": "Rent during the leak",
              "request_expense_compensation": "yes",
              "request_expense_compensation_reason": "Dehumidifier"}
    assert counter(TENANT, **values) == "5 of 12 fields"
    assert counter(TENANT, **{**values, "request_payment": ""}) == "0 of 7 fields"
    assert counter(TENANT, **values) == "5 of 12 fields"


def test_other_f3_requests_are_not_payment_children() -> None:
    assert counter(TENANT, request_repairs="yes", request_repairs_reason="Fix the ceiling") == (
        "2 of 9 fields")


# ------------------------------------------------------------------------- unchanged counting rule


@BOTH_ROLES
def test_required_fields_stay_in_the_total_and_empty_optional_ones_stay_out(
    landlord: bool, base: int,
) -> None:
    assert counter(landlord, tenant_name="Riley Example") == line(1, base)
    # A visible empty optional field (Who gave notice? after Yes) still does not count.
    assert counter(landlord, notice_given="yes") == line(1, base + 1)


def test_required_fields_are_never_hidden() -> None:
    for landlord in (LANDLORD, TENANT):
        payment = "landlord_request_payment" if landlord else "request_payment"
        fields = draft(landlord, notice_given="no", deposit_paid="no", **{payment: ""})
        keys = set(shown(landlord, fields))
        assert {f["key"] for f in fields if f["required"]} <= keys


def test_the_other_roles_requests_never_apply() -> None:
    tenant_fields = draft(TENANT) + [{"key": "landlord_request_payment", "label": "x",
                                      "required": False, "status": "user_entered", "value": "yes"},
                                     {"key": "landlord_request_payment_reason", "label": "x",
                                      "required": False, "status": "missing", "value": None}]
    assert "landlord_request_payment" not in shown(TENANT, tenant_fields)
    assert "request_payment" not in shown(LANDLORD, draft(TENANT))


def test_a_request_without_its_reason_field_is_not_shown() -> None:
    fields = [f for f in draft(TENANT, request_repairs="yes")
              if f["key"] != "request_repairs_reason"]
    assert "request_repairs" not in shown(TENANT, fields)


# ----------------------------------------------------------------------- same rule as the server


def test_the_page_and_the_server_have_the_same_f2_dependencies() -> None:
    page = js("Object.fromEntries(Object.entries(leaseFields).filter(([, d]) => d.when)"
              ".map(([k, d]) => [k, d.when]))")
    assert page == {key: list(dep) for key, dep in F2_DEPENDENCIES.items()}


def test_the_page_and_the_server_have_the_same_payment_children() -> None:
    page = js("[Object.keys(paymentRequests).map(k => `request_${k}`), "
              "Object.keys(landlordPaymentRequests).map(k => `landlord_request_${k}`), "
              "Object.keys(tenantRequests).map(k => `request_${k}`), "
              "Object.keys(landlordRequests).map(k => `landlord_request_${k}`)]")
    f3_children, f4_children, f3_top, f4_top = page  # type: ignore[misc]
    assert set(f3_children) == F3_PAYMENT_REQUESTS
    assert set(f4_children) == F4_PAYMENT_REQUESTS
    assert set(f3_top) | set(f3_children) == set(F3_REQUESTS)
    assert set(f4_top) | set(f4_children) == set(F4_REQUESTS)
