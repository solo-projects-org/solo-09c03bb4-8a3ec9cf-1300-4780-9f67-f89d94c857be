"""Functional tests for dual-cohort overlap checks.

Covers: distinct-subject intersection of both current revisions, small-cell
suppression ("少于5"), no identity/date leakage, order-independent single
conclusion per pair, immediate invalidation on new revisions, late-scan
discard under revision interleaving, failure/restart recovery, and
unaffected package downloads for unrelated cohorts.
"""
from __future__ import annotations

import io

import pytest


def _csv_bytes(header: str, rows: list[tuple]) -> bytes:
    buf = io.StringIO()
    buf.write(header + "\n")
    for r in rows:
        buf.write(",".join(str(x) for x in r) + "\n")
    return buf.getvalue().encode("utf-8")


def _upload(client, cohort: str, revision: str, payload: bytes):
    return client.post(
        f"/api/cohorts/{cohort}/revisions/{revision}",
        files={"file": ("rev.csv", payload, "text/csv")},
    )


def _request_check(client, a: str, b: str):
    return client.post(f"/api/overlap-checks/{a}/{b}")


def _get_check(client, a: str, b: str):
    return client.get(f"/api/overlap-checks/{a}/{b}")


def _wait_check_done(app_env, a: str, b: str, max_ticks: int = 20) -> dict:
    """Tick the worker (packages interleave too) until the pair concludes."""
    client, tick = app_env["client"], app_env["tick"]
    for _ in range(max_ticks):
        view = _get_check(client, a, b).json()
        if view["status"] == "done":
            return view
        tick()
    raise AssertionError(f"overlap check for {a}/{b} did not complete")


def test_overlap_counts_distinct_subjects_and_suppresses_below_five(app_env):
    client = app_env["client"]
    # cohort-a: P001..P007, with P001/P002 visiting twice (must count once).
    rows_a = [(f"P{i:03d}", "2026-01-10", 1.0) for i in range(1, 8)]
    rows_a += [("P001", "2026-02-10", 2.0), ("P002", "2026-02-11", 3.0)]
    # cohort-b: P003..P006, each with two visit rows.
    rows_b = []
    for i in range(3, 7):
        rows_b += [(f"P{i:03d}", "2026-01-15", 1.0), (f"P{i:03d}", "2026-02-15", 2.0)]
    assert _upload(client, "cohort-a", "1", _csv_bytes("受试者号,访视日期,测值", rows_a)).status_code == 202
    assert _upload(client, "cohort-b", "1", _csv_bytes("subject_id,visit_date,value", rows_b)).status_code == 202

    resp = _request_check(client, "cohort-a", "cohort-b")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["revision_a"] == "1" and body["revision_b"] == "1"
    assert body["overlap_count"] is None and body["overlap_display"] is None

    # Distinct overlap is {P003..P006} = 4 subjects -> suppressed as "少于5".
    # (A naive row-level count would reach 8 and not be suppressed.)
    view = _wait_check_done(app_env, "cohort-a", "cohort-b")
    assert view["current"] is True
    assert view["suppressed"] is True
    assert view["overlap_count"] is None
    assert view["overlap_display"] == "少于5"


def test_overlap_reports_exact_count_at_five_or_more(app_env):
    client = app_env["client"]
    rows_a = [(f"P{i:03d}", "2026-01-10", 1.0) for i in range(1, 8)]  # P001..P007
    rows_b = [(f"P{i:03d}", "2026-01-12", 1.0) for i in range(3, 10)]  # P003..P009
    _upload(client, "cohort-a", "1", _csv_bytes("subject_id,visit_date,value", rows_a))
    _upload(client, "cohort-b", "1", _csv_bytes("subject_id,visit_date,value", rows_b))

    assert _request_check(client, "cohort-a", "cohort-b").status_code == 202
    view = _wait_check_done(app_env, "cohort-a", "cohort-b")
    assert view["current"] is True
    assert view["suppressed"] is False
    assert view["overlap_count"] == 5  # {P003..P007}
    assert view["overlap_display"] == "5"


def test_overlap_endpoints_never_leak_identity_or_dates(app_env):
    client = app_env["client"]
    ids_a = [f"SUBJ-ALPHA-{i:03d}" for i in range(1, 8)]
    ids_b = [f"SUBJ-ALPHA-{i:03d}" for i in range(3, 10)]
    secret_dates = ["2026-03-07", "2026-04-19"]
    rows_a = [(sid, secret_dates[0], 1.0) for sid in ids_a]
    rows_b = [(sid, secret_dates[1], 1.0) for sid in ids_b]
    _upload(client, "cohort-a", "1", _csv_bytes("subject_id,visit_date,value", rows_a))
    _upload(client, "cohort-b", "1", _csv_bytes("subject_id,visit_date,value", rows_b))

    responses = [_request_check(client, "cohort-a", "cohort-b")]
    _wait_check_done(app_env, "cohort-a", "cohort-b")
    responses.append(_get_check(client, "cohort-a", "cohort-b"))
    responses.append(_get_check(client, "cohort-b", "cohort-a"))
    responses.append(client.get("/api/overlap-checks"))
    responses.append(_request_check(client, "cohort-b", "cohort-a"))  # idempotent re-request

    for resp in responses:
        assert resp.status_code in (200, 202)
        assert "SUBJ-ALPHA" not in resp.text
        for d in secret_dates:
            assert d not in resp.text


def test_pair_is_order_independent_with_single_current_conclusion(app_env):
    client = app_env["client"]
    rows_a = [(f"P{i:03d}", "2026-01-10", 1.0) for i in range(1, 8)]
    rows_b = [(f"P{i:03d}", "2026-01-12", 1.0) for i in range(3, 10)]
    _upload(client, "cohort-a", "1", _csv_bytes("subject_id,visit_date,value", rows_a))
    _upload(client, "cohort-b", "1", _csv_bytes("subject_id,visit_date,value", rows_b))

    r1 = _request_check(client, "cohort-a", "cohort-b")
    assert r1.status_code == 202
    # Reversed order addresses the same row: no duplicate is created.
    r2 = _request_check(client, "cohort-b", "cohort-a")
    assert r2.status_code == 200
    assert r2.json()["status"] == "pending"

    view_ab = _wait_check_done(app_env, "cohort-a", "cohort-b")
    view_ba = _get_check(client, "cohort-b", "cohort-a").json()
    assert view_ab == view_ba
    assert view_ab["current"] is True and view_ab["overlap_count"] == 5

    listed = client.get("/api/overlap-checks").json()["checks"]
    pair_rows = [
        c for c in listed
        if {c["cohort_a"], c["cohort_b"]} == {"cohort-a", "cohort-b"}
    ]
    assert len(pair_rows) == 1
    assert pair_rows[0]["current"] is True


def test_new_revision_immediately_withdraws_conclusion_and_rechecks(app_env):
    client = app_env["client"]
    rows_a1 = [(f"P{i:03d}", "2026-01-10", 1.0) for i in range(1, 6)]  # P001..P005
    rows_b = [(f"P{i:03d}", "2026-01-12", 1.0) for i in range(1, 6)]   # P001..P005
    _upload(client, "cohort-a", "1", _csv_bytes("subject_id,visit_date,value", rows_a1))
    _upload(client, "cohort-b", "1", _csv_bytes("subject_id,visit_date,value", rows_b))
    _request_check(client, "cohort-a", "cohort-b")
    view = _wait_check_done(app_env, "cohort-a", "cohort-b")
    assert view["current"] is True and view["overlap_count"] == 5

    # cohort-a accepts rev2 (only P001..P003): the old conclusion loses its
    # currency immediately, before any new scan runs.
    rows_a2 = [(f"P{i:03d}", "2026-02-10", 1.0) for i in range(1, 4)]
    assert _upload(client, "cohort-a", "2", _csv_bytes("subject_id,visit_date,value", rows_a2)).status_code == 202
    view = _get_check(client, "cohort-a", "cohort-b").json()
    assert view["status"] == "pending"
    assert view["current"] is False
    assert view["overlap_count"] is None and view["overlap_display"] is None
    assert view["revision_a"] == "2" and view["revision_b"] == "1"
    listed = client.get("/api/overlap-checks").json()["checks"]
    assert listed[0]["status"] == "pending" and listed[0]["overlap_display"] is None

    # The re-armed check concludes against the new revisions: overlap 3 -> suppressed.
    view = _wait_check_done(app_env, "cohort-a", "cohort-b")
    assert view["current"] is True
    assert view["revision_a"] == "2" and view["revision_b"] == "1"
    assert view["suppressed"] is True and view["overlap_display"] == "少于5"


def test_late_scan_result_cannot_become_current_conclusion(app_env):
    """Revision interleaving: a revision accepted mid-scan invalidates the
    in-flight scan; its late result must not become the current conclusion."""
    client = app_env["client"]
    checks = app_env["modules"]["checks"]
    rows_a1 = [(f"P{i:03d}", "2026-01-10", 1.0) for i in range(1, 6)]  # P001..P005
    rows_b = [(f"P{i:03d}", "2026-01-12", 1.0) for i in range(1, 6)]
    _upload(client, "cohort-a", "1", _csv_bytes("subject_id,visit_date,value", rows_a1))
    _upload(client, "cohort-b", "1", _csv_bytes("subject_id,visit_date,value", rows_b))
    assert _request_check(client, "cohort-a", "cohort-b").status_code == 202

    # Worker claims the check (status checking) but has not finalised yet.
    claim = checks.claim_pending_check()
    assert claim == ("cohort-a", "cohort-b", 1, 1)

    # Mid-scan, cohort-a accepts rev2: the pair is re-armed to pending.
    rows_a2 = [(f"P{i:03d}", "2026-02-10", 1.0) for i in range(1, 4)]  # P001..P003
    assert _upload(client, "cohort-a", "2", _csv_bytes("subject_id,visit_date,value", rows_a2)).status_code == 202
    view = _get_check(client, "cohort-a", "cohort-b").json()
    assert view["status"] == "pending"
    assert view["revision_a"] == "2" and view["revision_b"] == "1"

    # The in-flight scan for the old revisions finishes late: discarded.
    with pytest.raises(checks.StaleCheckError):
        checks.finalize_check("cohort-a", "cohort-b", 1, 1)
    view = _get_check(client, "cohort-a", "cohort-b").json()
    assert view["status"] == "pending"
    assert view["overlap_display"] is None

    # The re-armed check concludes from the new revisions only.
    view = _wait_check_done(app_env, "cohort-a", "cohort-b")
    assert view["current"] is True
    assert view["revision_a"] == "2"
    assert view["overlap_display"] == "少于5"  # overlap {P001..P003} = 3


def test_failed_check_stays_visible_re_arms_and_recovers_after_restart(app_env, monkeypatch):
    client = app_env["client"]
    checks = app_env["modules"]["checks"]
    worker_mod = app_env["modules"]["worker"]
    database = app_env["modules"]["database"]
    rows = [(f"P{i:03d}", "2026-01-10", 1.0) for i in range(1, 6)]
    _upload(client, "cohort-a", "1", _csv_bytes("subject_id,visit_date,value", rows))
    _upload(client, "cohort-b", "1", _csv_bytes("subject_id,visit_date,value", rows))
    _request_check(client, "cohort-a", "cohort-b")

    calls = {"n": 0}
    real_finalize = checks.finalize_check

    def flaky(a, b, rev_a, rev_b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated scan failure")
        return real_finalize(a, b, rev_a, rev_b)

    monkeypatch.setattr(worker_mod, "finalize_check", flaky)

    # Drain until the check fails (packages build alongside).
    for _ in range(10):
        view = _get_check(client, "cohort-a", "cohort-b").json()
        if view["status"] == "failed":
            break
        app_env["tick"]()
    assert view["status"] == "failed"
    assert "simulated scan failure" in view["last_error"]
    assert view["overlap_display"] is None
    # Failure is visible in the list endpoint as well.
    assert client.get("/api/overlap-checks").json()["checks"][0]["status"] == "failed"

    # Manual re-arm via a fresh request (backoff would otherwise delay it).
    resp = _request_check(client, "cohort-a", "cohort-b")
    assert resp.status_code == 202 and resp.json()["status"] == "pending"
    view = _wait_check_done(app_env, "cohort-a", "cohort-b")
    assert view["current"] is True and view["overlap_count"] == 5

    # Crash mid-scan: a row stuck in checking returns to pending at startup.
    conn = database.get_conn()
    conn.execute("UPDATE overlap_checks SET status='checking'")
    conn.close()
    assert database.reset_interrupted_checks() == 1
    assert _get_check(client, "cohort-a", "cohort-b").json()["status"] == "pending"

    # A failure recorded before the crash is also retried without delay.
    conn = database.get_conn()
    conn.execute("UPDATE overlap_checks SET status='failed', last_error='boom'")
    conn.close()
    assert database.reset_interrupted_checks() == 1
    view = _get_check(client, "cohort-a", "cohort-b").json()
    assert view["status"] == "pending"
    assert view["last_error"] == "boom"  # error note kept, state is to-be-checked


def test_unrelated_cohort_package_download_unaffected_by_checks(app_env):
    client = app_env["client"]
    rows = [(f"P{i:03d}", "2026-01-10", 1.0) for i in range(1, 6)]
    for cohort in ("cohort-a", "cohort-b", "cohort-c"):
        _upload(client, cohort, "1", _csv_bytes("subject_id,visit_date,value", rows))
    _request_check(client, "cohort-a", "cohort-b")

    # Drain everything: 3 packages + 1 check.
    for _ in range(8):
        app_env["tick"]()

    for cohort in ("cohort-a", "cohort-b", "cohort-c"):
        resp = client.get(f"/api/cohorts/{cohort}/package")
        assert resp.status_code == 200, (cohort, resp.text)
        assert resp.headers["content-type"] == "application/zip"
    assert _get_check(client, "cohort-a", "cohort-b").json()["current"] is True
    # Pairs never requested have no record.
    assert _get_check(client, "cohort-a", "cohort-c").status_code == 404


def test_overlap_request_validation(app_env):
    client = app_env["client"]
    rows = [("P001", "2026-01-10", 1.0)]
    _upload(client, "cohort-a", "1", _csv_bytes("subject_id,visit_date,value", rows))

    # Same cohort twice is not a dual-cohort check.
    assert _request_check(client, "cohort-a", "cohort-a").status_code == 400
    # Unknown cohort on either side.
    assert _request_check(client, "cohort-a", "ghost-b").status_code == 404
    assert _request_check(client, "ghost-b", "cohort-a").status_code == 404
    # Unsafe identifiers.
    assert _request_check(client, "bad cohort", "cohort-a").status_code == 400
    # No check record yet for this pair.
    assert _get_check(client, "cohort-a", "other").status_code == 404
    # Nothing was persisted by the rejected requests.
    assert client.get("/api/overlap-checks").json()["checks"] == []


def test_done_conclusion_is_current_only_while_revisions_unchanged(app_env):
    """Read-time guard: even a stored done conclusion is not reported as
    current once either cohort's revision no longer matches."""
    client = app_env["client"]
    database = app_env["modules"]["database"]
    rows = [(f"P{i:03d}", "2026-01-10", 1.0) for i in range(1, 6)]
    _upload(client, "cohort-a", "1", _csv_bytes("subject_id,visit_date,value", rows))
    _upload(client, "cohort-b", "1", _csv_bytes("subject_id,visit_date,value", rows))
    _request_check(client, "cohort-a", "cohort-b")
    view = _wait_check_done(app_env, "cohort-a", "cohort-b")
    assert view["current"] is True and view["overlap_count"] == 5

    # Force the revisions out of sync (bypassing accept_revision's re-arm).
    conn = database.get_conn()
    conn.execute("UPDATE cohorts SET current_revision = 99 WHERE cohort_id = 'cohort-a'")
    conn.close()

    for resp in (_get_check(client, "cohort-a", "cohort-b"), client.get("/api/overlap-checks")):
        payload = resp.json()
        item = payload if "status" in payload else payload["checks"][0]
        assert item["status"] == "done"
        assert item["current"] is False
        assert item["overlap_count"] is None
        assert item["overlap_display"] is None
