"""Package gating and stage rules (BUILD_PLAN sections 3 and 6). Pure unit tests: no DB, no LLM."""

import pytest

from app.services.gating import MissingField, OpenGap, compute_stage, package_blockers

GAP_A = OpenGap(title="Lease copy", field_key="lease_start")
GAP_B = OpenGap(title="Repair request", field_key=None)
FIELD_START = MissingField(key="lease_start", label="Lease start date")
FIELD_UNIT = MissingField(key="unit_number", label="Unit number")


# --- package_blockers -------------------------------------------------------------------


def test_ready_package_has_no_blockers():
    assert package_blockers([], [], tenant_attested=True) == []


@pytest.mark.parametrize(
    ("gaps", "expected"),
    [
        ([GAP_A], ["Required item still open: Lease copy"]),
        (
            [GAP_A, GAP_B],
            ["Required item still open: Lease copy", "Required item still open: Repair request"],
        ),
    ],
)
def test_each_open_blocking_gap_gives_a_reason(gaps, expected):
    assert package_blockers(gaps, [], tenant_attested=True) == expected


def test_missing_required_field_gives_a_reason():
    assert package_blockers([], [FIELD_UNIT], tenant_attested=True) == [
        "Required field is empty: Unit number"
    ]


def test_missing_field_covered_by_open_gap_is_not_listed_twice():
    reasons = package_blockers([GAP_A], [FIELD_START], tenant_attested=True)
    assert reasons == ["Required item still open: Lease copy"]


def test_gap_without_field_key_does_not_cover_a_field():
    reasons = package_blockers([GAP_B], [FIELD_START], tenant_attested=True)
    assert reasons == [
        "Required item still open: Repair request",
        "Required field is empty: Lease start date",
    ]


def test_only_the_covered_field_is_suppressed():
    reasons = package_blockers([GAP_A], [FIELD_START, FIELD_UNIT], tenant_attested=True)
    assert reasons == [
        "Required item still open: Lease copy",
        "Required field is empty: Unit number",
    ]


def test_unattested_tenant_gives_a_reason():
    assert package_blockers([], [], tenant_attested=False) == [
        "The tenant hasn't confirmed their files are genuine"
    ]


def test_all_three_kinds_combine_in_order_gaps_fields_tenant():
    reasons = package_blockers([GAP_B], [FIELD_UNIT], tenant_attested=False)
    assert reasons == [
        "Required item still open: Repair request",
        "Required field is empty: Unit number",
        "The tenant hasn't confirmed their files are genuine",
    ]


# --- compute_stage ----------------------------------------------------------------------


@pytest.mark.parametrize("waiting", [True, False])
@pytest.mark.parametrize(("blocking", "pending"), [(0, 0), (2, 0), (0, 3), (1, 1)])
def test_hearing_scheduled_is_always_preserved(waiting, blocking, pending):
    stage = compute_stage(
        "hearing_scheduled",
        open_blocking=blocking,
        pending_review=pending,
        waiting_on_tenant=waiting,
    )
    assert stage == "hearing_scheduled"


@pytest.mark.parametrize("current", ["needs_review", "ready_to_file", "waiting_tenant"])
@pytest.mark.parametrize(("blocking", "pending"), [(0, 0), (2, 0), (0, 1)])
def test_waiting_on_tenant_gives_waiting_tenant(current, blocking, pending):
    stage = compute_stage(
        current, open_blocking=blocking, pending_review=pending, waiting_on_tenant=True
    )
    assert stage == "waiting_tenant"


@pytest.mark.parametrize("current", ["needs_review", "ready_to_file", "waiting_tenant"])
def test_zero_blocking_and_zero_pending_is_ready_to_file(current):
    stage = compute_stage(current, open_blocking=0, pending_review=0, waiting_on_tenant=False)
    assert stage == "ready_to_file"


@pytest.mark.parametrize(
    ("blocking", "pending"),
    [(1, 0), (0, 1), (3, 2)],
)
def test_any_blocking_or_pending_is_needs_review(blocking, pending):
    stage = compute_stage(
        "ready_to_file",
        open_blocking=blocking,
        pending_review=pending,
        waiting_on_tenant=False,
    )
    assert stage == "needs_review"


def test_waiting_tenant_with_nothing_left_becomes_ready_to_file():
    stage = compute_stage(
        "waiting_tenant", open_blocking=0, pending_review=0, waiting_on_tenant=False
    )
    assert stage == "ready_to_file"


@pytest.mark.parametrize(("blocking", "pending"), [(1, 0), (0, 2), (1, 1)])
def test_waiting_tenant_with_items_left_becomes_needs_review(blocking, pending):
    stage = compute_stage(
        "waiting_tenant",
        open_blocking=blocking,
        pending_review=pending,
        waiting_on_tenant=False,
    )
    assert stage == "needs_review"
