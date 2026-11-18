"""Functional tests for revision intake, package content and download rules."""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime

import pytest


def _csv_bytes(header: str, rows: list[tuple]) -> bytes:
    buf = io.StringIO()
    buf.write(header + "\n")
    for r in rows:
        buf.write(",".join(str(x) for x in r) + "\n")
    return buf.getvalue().encode("utf-8")


def _upload(client, cohort: str, revision: str, payload: bytes, filename: str = "rev.csv"):
    return client.post(
        f"/api/cohorts/{cohort}/revisions/{revision}",
        files={"file": (filename, payload, "text/csv")},
    )


def _read_zip(resp_bytes: bytes) -> tuple[list[dict], dict]:
    zf = zipfile.ZipFile(io.BytesIO(resp_bytes))
    assert set(zf.namelist()) == {"deidentified_visits.csv", "manifest.json"}
    rows = list(csv.DictReader(io.StringIO(zf.read("deidentified_visits.csv").decode("utf-8"))))
    import json

    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    return rows, manifest


def _wait_published(app_env, cohort: str, max_ticks: int = 5):
    tick = app_env["tick"]
    for _ in range(max_ticks):
        status = app_env["client"].get(f"/api/cohorts/{cohort}").json()
        if status["current_package"]["status"] == "published":
            return status
        tick()
    raise AssertionError("package did not reach published state")


ROWS_A = [
    ("P001", "2026-01-10", 12.5),
    ("P001", "2026-02-10", 13.0),
    ("P002", "2026-01-15", 7.2),
    ("P002", "2026-03-15", 8.1),
    ("P003", "2026-02-01", 9.9),
]


def test_end_to_end_package_is_deidentified_and_downloadable(app_env):
    client = app_env["client"]
    payload = _csv_bytes("受试者号,访视日期,测值", ROWS_A)

    resp = _upload(client, "cohort-a", "1", payload)
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "accepted"

    # Before the worker runs: package pending and not downloadable.
    status = client.get("/api/cohorts/cohort-a").json()
    assert status["current_package"]["status"] == "pending"
    dl = client.get("/api/cohorts/cohort-a/package")
    assert dl.status_code == 409

    _wait_published(app_env, "cohort-a")

    dl = client.get("/api/cohorts/cohort-a/package")
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/zip"
    rows, manifest = _read_zip(dl.content)
    assert manifest["row_count"] == 5
    assert manifest["subject_count"] == 3

    # Original identity / dates must not leak anywhere in the archive.
    raw_archive = dl.content
    for original_id in ("P001", "P002", "P003"):
        assert original_id.encode() not in raw_archive
    for original_date in ("2026-01-10", "2026-02-10", "2026-01-15", "2026-03-15", "2026-02-01"):
        assert original_date.encode() not in raw_archive

    # Pseudonyms are stable cohort-scoped ids, values preserved.
    pseudos = {r["pseudonym"] for r in rows}
    assert pseudos == {"S000001", "S000002", "S000003"}
    values = sorted(float(r["value"]) for r in rows)
    assert values == sorted(float(r[2]) for r in ROWS_A)

    # Within-subject visit intervals must be exactly preserved after shifting.
    by_pseudo: dict[str, list[date]] = {}
    for r in rows:
        by_pseudo.setdefault(r["pseudonym"], []).append(
            datetime.strptime(r["visit_date_shifted"], "%Y-%m-%d").date()
        )
    original_intervals = {
        "P001": (date(2026, 2, 10) - date(2026, 1, 10)).days,
        "P002": (date(2026, 3, 15) - date(2026, 1, 15)).days,
    }
    shifted_p1 = by_pseudo["S000001"]
    shifted_p2 = by_pseudo["S000002"]
    assert abs((shifted_p1[1] - shifted_p1[0]).days) == original_intervals["P001"]
    assert abs((shifted_p2[1] - shifted_p2[0]).days) == original_intervals["P002"]


def test_stable_pseudonym_and_shift_across_revisions(app_env):
    client, tick = app_env["client"], app_env["tick"]
    _upload(client, "cohort-b", "1", _csv_bytes("subject_id,visit_date,value", ROWS_A))
    _wait_published(app_env, "cohort-b")
    first = _read_zip(client.get("/api/cohorts/cohort-b/package").content)[0]

    # Revision 2: same subjects, one new subject, visits moved forward by ~30d.
    rows_b = [
        ("P001", "2026-03-12", 14.1),
        ("P002", "2026-04-14", 8.8),
        ("P003", "2026-03-03", 10.0),
        ("P004", "2026-03-20", 6.0),
    ]
    resp = _upload(client, "cohort-b", "2", _csv_bytes("subject_id,visit_date,value", rows_b))
    assert resp.status_code == 202

    # Accepting rev2 immediately withdraws the old package's current eligibility.
    status = client.get("/api/cohorts/cohort-b").json()
    assert status["current_revision"] == "2"
    pkg_states = {p["revision"]: p["status"] for p in status["packages"]}
    assert pkg_states["1"] == "superseded"
    assert pkg_states["2"] == "pending"
    dl = client.get("/api/cohorts/cohort-b/package")
    assert dl.status_code == 409

    _wait_published(app_env, "cohort-b")
    second = _read_zip(client.get("/api/cohorts/cohort-b/package").content)[0]

    first_map = {r["pseudonym"]: r["visit_date_shifted"] for r in first}
    second_map = {r["pseudonym"]: r["visit_date_shifted"] for r in second}

    # Existing subjects keep their pseudonym and their fixed shift: the
    # difference between original and shifted date is constant per subject.
    db = app_env["modules"]["database"].get_conn()
    try:
        identities = {
            r["subject_id"]: (r["pseudonym"], r["date_shift_days"])
            for r in db.execute("SELECT subject_id, pseudonym, date_shift_days FROM subjects").fetchall()
        }
    finally:
        db.close()

    for sid, original in [("P001", date(2026, 3, 12)), ("P002", date(2026, 4, 14)),
                          ("P003", date(2026, 3, 3))]:
        pseudo, shift = identities[sid]
        expected = (original.toordinal() + shift)
        assert datetime.strptime(second_map[pseudo], "%Y-%m-%d").date().toordinal() == expected
        assert pseudo in first_map  # pseudonym unchanged across revisions

    # New subject takes the next sequential pseudonym.
    assert identities["P004"][0] == "S000004"


def test_cohorts_have_independent_identities_and_shifts(app_env):
    client = app_env["client"]
    shared_rows = [("P001", "2026-01-10", 1.0), ("P001", "2026-02-10", 2.0)]
    _upload(client, "cohort-x", "1", _csv_bytes("subject_id,visit_date,value", shared_rows))
    _upload(client, "cohort-y", "1", _csv_bytes("subject_id,visit_date,value", shared_rows))
    _wait_published(app_env, "cohort-x")
    _wait_published(app_env, "cohort-y")

    pkg_x = _read_zip(client.get("/api/cohorts/cohort-x/package").content)[0]
    pkg_y = _read_zip(client.get("/api/cohorts/cohort-y/package").content)[0]

    # Same original subject -> same pseudonym index is fine, but per-cohort
    # salts must (overwhelmingly likely) produce different shifted dates.
    dates_x = sorted(r["visit_date_shifted"] for r in pkg_x)
    dates_y = sorted(r["visit_date_shifted"] for r in pkg_y)
    assert dates_x != dates_y

    # Intervals remain identical within each cohort.
    def interval(pkg):
        d = sorted(datetime.strptime(r["visit_date_shifted"], "%Y-%m-%d").date() for r in pkg)
        return (d[1] - d[0]).days

    assert interval(pkg_x) == interval(pkg_y) == 31

    # Cohort list exposes both independently.
    cohorts = {c["cohort_id"]: c for c in client.get("/api/cohorts").json()["cohorts"]}
    assert set(cohorts) == {"cohort-x", "cohort-y"}
    assert cohorts["cohort-x"]["current_package"]["download_available"] is True


def test_conflict_rejected_and_identical_reupload_idempotent(app_env):
    client = app_env["client"]
    r1 = _upload(client, "cohort-c", "1", _csv_bytes("subject_id,visit_date,value", ROWS_A))
    assert r1.status_code == 202

    # Same revision number, different content -> 409 and nothing changes.
    conflicting = ROWS_A[:-1] + [("P003", "2026-02-01", 11.11)]
    r2 = _upload(client, "cohort-c", "1", _csv_bytes("subject_id,visit_date,value", conflicting))
    assert r2.status_code == 409
    status = client.get("/api/cohorts/cohort-c").json()
    assert len(status["revisions"]) == 1
    assert status["revisions"][0]["row_count"] == 5

    # Byte-identical re-upload -> idempotent 200, still one revision.
    r3 = _upload(client, "cohort-c", "1", _csv_bytes("subject_id,visit_date,value", ROWS_A))
    assert r3.status_code == 200
    assert r3.json()["status"] == "duplicate"
    assert len(client.get("/api/cohorts/cohort-c").json()["revisions"]) == 1


def test_failure_retries_and_restart_recovery(app_env, monkeypatch):
    client, tick = app_env["client"], app_env["tick"]
    services = app_env["modules"]["services"]
    _upload(client, "cohort-d", "1", _csv_bytes("subject_id,visit_date,value", ROWS_A))

    calls = {"n": 0}
    real_finalize = services.finalize_package

    def flaky(cohort_id, revision):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated build failure")
        return real_finalize(cohort_id, revision)

    # run_once() resolves finalize_package through the worker module globals.
    import app.worker as worker_mod
    monkeypatch.setattr(worker_mod, "finalize_package", flaky)

    assert tick() is True  # first attempt fails
    status = client.get("/api/cohorts/cohort-d").json()
    pkg = status["current_package"]
    assert pkg["status"] == "failed"
    assert "simulated build failure" in pkg["last_error"]
    # Failed package is still not downloadable.
    assert client.get("/api/cohorts/cohort-d/package").status_code == 409

    # Backoff would block the automatic retry; the retry endpoint re-arms it.
    resp = client.post("/api/cohorts/cohort-d/retry")
    assert resp.status_code == 200 and resp.json()["status"] == "pending"
    assert tick() is True  # second attempt succeeds
    _wait_published(app_env, "cohort-d")
    assert client.get("/api/cohorts/cohort-d/package").status_code == 200

    # Simulate a crash mid-build: reset recovery must return it to pending.
    db = app_env["modules"]["database"]
    conn = db.get_conn()
    conn.execute("UPDATE packages SET status='generating' WHERE cohort_id='cohort-d'")
    conn.close()
    recovered = db.reset_interrupted_packages()
    assert recovered >= 1
    status = client.get("/api/cohorts/cohort-d").json()
    assert status["current_package"]["status"] == "pending"
    assert client.get("/api/cohorts/cohort-d/package").status_code == 409


def test_late_build_cannot_overwrite_newer_revision(app_env, monkeypatch):
    client, tick = app_env["client"], app_env["tick"]
    services = app_env["modules"]["services"]
    _upload(client, "cohort-e", "1", _csv_bytes("subject_id,visit_date,value", ROWS_A))
    assert tick() is True  # rev1 builds and publishes

    # Rev2 arrives; claim it (generating) but block the build while rev3 lands.
    _upload(client, "cohort-e", "2",
            _csv_bytes("subject_id,visit_date,value", ROWS_A + [("P009", "2026-05-01", 1.5)]))
    claim = services.claim_pending_package()
    assert claim == ("cohort-e", 2)

    _upload(client, "cohort-e", "3",
            _csv_bytes("subject_id,visit_date,value", ROWS_A + [("P010", "2026-06-01", 2.5)]))
    # rev2's claim is now stale; its finalize must be discarded, not published.
    with pytest.raises(services.StaleRevisionError):
        services.finalize_package("cohort-e", "2")

    status = client.get("/api/cohorts/cohort-e").json()
    states = {p["revision"]: p["status"] for p in status["packages"]}
    assert states["2"] == "superseded"
    assert states["3"] == "pending"
    assert status["current_revision"] == "3"

    # No artifact for the stale build may exist on disk.
    pkg_dir = app_env["modules"]["config"].PACKAGE_DIR / "cohort-e"
    assert not (pkg_dir / "rev2_deid.zip").exists()

    # Drain the queue: rev3 publishes normally and becomes the only download.
    _wait_published(app_env, "cohort-e")
    dl = client.get("/api/cohorts/cohort-e/package")
    assert dl.status_code == 200
    rows, _ = _read_zip(dl.content)
    # P010 appears via rev3; P009's identity from the superseded rev2 still
    # occupies S000004, so P010 takes the next pseudonym.
    pseudos = {r["pseudonym"] for r in rows}
    assert "S000005" in pseudos
    assert len(rows) == 6


def test_other_cohort_download_unaffected_while_one_generates(app_env):
    client, tick = app_env["client"], app_env["tick"]
    _upload(client, "cohort-q1", "1", _csv_bytes("subject_id,visit_date,value", ROWS_A))
    assert tick() is True  # q1 published

    _upload(client, "cohort-q1", "2",
            _csv_bytes("subject_id,visit_date,value", ROWS_A + [("P020", "2026-07-01", 3.3)]))
    _upload(client, "cohort-q2", "1", _csv_bytes("subject_id,visit_date,value", ROWS_A))

    # q1 rev2 pending; meanwhile q2 builds. q1's old package is superseded.
    assert client.get("/api/cohorts/cohort-q1/package").status_code == 409
    assert tick() is True  # whichever is due first
    assert tick() is True  # drain both
    assert client.get("/api/cohorts/cohort-q1/package").status_code == 200
    assert client.get("/api/cohorts/cohort-q2/package").status_code == 200


def test_invalid_csv_and_identifiers_rejected(app_env):
    client = app_env["client"]
    # Missing required column.
    bad_header = _csv_bytes("subject_id,visit_date,foo", [("P1", "2026-01-01", 1.0)])
    assert _upload(client, "cohort-z", "1", bad_header).status_code == 400
    # Bad date.
    bad_date = _csv_bytes("subject_id,visit_date,value", [("P1", "01/01/2026", 1.0)])
    assert _upload(client, "cohort-z", "1", bad_date).status_code == 400
    # Non-numeric value.
    bad_value = _csv_bytes("subject_id,visit_date,value", [("P1", "2026-01-01", "abc")])
    assert _upload(client, "cohort-z", "1", bad_value).status_code == 400
    # Empty data section.
    empty = _csv_bytes("subject_id,visit_date,value", [])
    assert _upload(client, "cohort-z", "1", empty).status_code == 400
    # Unsafe cohort identifiers: characters outside the allowed set or too long.
    good = _csv_bytes("subject_id,visit_date,value", [("P1", "2026-01-01", 1.0)])
    assert _upload(client, "bad cohort", "1", good).status_code == 400
    assert _upload(client, "x" * 129, "1", good).status_code == 400
    # Nothing was accepted.
    assert client.get("/api/cohorts").json()["cohorts"] == []
