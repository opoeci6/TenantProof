"""Concurrent package builds on one case must get distinct, consecutive versions."""

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlmodel import col, select

from app.models import ActivityRow, PackageRow
from tests.conftest import CASEWORKER, _client

CASE = "case_demo_001"
BUILD = f"/api/cases/{CASE}/package"
ATTESTED = {"attestations": {
    "reviewed_fields": True, "remedy_confirmed_with_tenant": True,
    "explained_not_legal_advice": True,
}}
BUILDERS = 4


def _make_ready(client) -> None:
    for gap, value in (("gap_01", "12 Harbour Rd"), ("gap_02", "Repair the leak")):
        r = client.patch(f"/api/gaps/{gap}", json={"status": "resolved", "value": value})
        assert r.status_code == 200, r.text


def test_concurrent_builds_get_distinct_consecutive_versions(fresh_db, session):
    clients = [_client(CASEWORKER) for _ in range(BUILDERS)]
    _make_ready(clients[0])
    barrier = threading.Barrier(BUILDERS)

    def build(client):
        barrier.wait()  # release every thread at once
        return client.post(BUILD, json=ATTESTED)

    with ThreadPoolExecutor(BUILDERS) as pool:
        responses = list(pool.map(build, clients))

    assert [r.status_code for r in responses] == [200] * BUILDERS, [r.text for r in responses]
    assert sorted(r.json()["version"] for r in responses) == list(range(1, BUILDERS + 1))

    session.expire_all()
    rows = session.exec(
        select(PackageRow).where(PackageRow.case_id == CASE).order_by(col(PackageRow.version))
    ).all()
    assert [r.version for r in rows] == list(range(1, BUILDERS + 1))
    assert len({r.sha256 for r in rows}) == BUILDERS  # four different zips, none overwritten
    built = [
        a for a in session.exec(select(ActivityRow).where(ActivityRow.case_id == CASE)).all()
        if a.action.startswith("Built filing package")
    ]
    assert len(built) == BUILDERS

    for row in rows:  # every version downloads, with the bytes its row promises
        r = clients[0].get(f"/api/cases/{CASE}/package/download", params={"version": row.version})
        assert r.status_code == 200
        assert hashlib.sha256(r.content).hexdigest() == row.sha256
