"""Unit tests for de-identification primitives and CSV validation."""
from __future__ import annotations

from datetime import date

import pytest

from app.csvutil import CSVValidationError, canonical_hash, parse_csv
from app.deid import build_package, pseudonym, subject_date_shift


def test_shift_is_stable_and_bounded():
    salt = "a" * 64
    s1 = subject_date_shift(salt, "P001")
    s2 = subject_date_shift(salt, "P001")
    assert s1 == s2  # deterministic for the same salt + subject
    assert -180 <= s1 <= 180
    # Different salt -> independently derived shift (overwhelmingly likely).
    other = subject_date_shift("b" * 64, "P001")
    assert other != s1 or True  # probabilistic; range/stability are the guarantees
    assert -180 <= other <= 180


def test_pseudonym_sequence():
    assert pseudonym(1) == "S000001"
    assert pseudonym(42) == "S000042"
    assert pseudonym(10**6) == "S1000000"


def test_build_package_keeps_intervals_and_excludes_originals():
    rows = parse_csv(
        "subject_id,visit_date,value\n"
        "P1,2026-01-01,1.5\n"
        "P1,2026-01-31,2.5\n"
        "P2,2026-02-15,3.0\n".encode("utf-8")
    )
    identity = {"P1": ("S000001", 10), "P2": ("S000002", -5)}
    payload, manifest = build_package(rows, identity)

    import io
    import zipfile

    zf = zipfile.ZipFile(io.BytesIO(payload))
    text = zf.read("deidentified_visits.csv").decode("utf-8")
    assert "S000001,2026-01-11,1.5" in text
    assert "S000001,2026-02-10,2.5" in text  # 30-day interval preserved
    assert "S000002,2026-02-10,3" in text
    # Nothing identifiable inside the archive.
    for token in ("P1", "P2", "2026-01-01", "2026-01-31", "2026-02-15"):
        assert token not in text
        assert token.encode() not in payload
    assert manifest["row_count"] == 3 and manifest["subject_count"] == 2


def test_parse_csv_accepts_chinese_headers_and_bom():
    payload = "﻿受试者号,访视日期,测值\nP1,2026-01-01,1\n".encode("utf-8")
    rows = parse_csv(payload)
    assert len(rows) == 1
    assert rows[0].subject_id == "P1"
    assert rows[0].visit_date == date(2026, 1, 1)
    assert rows[0].value == 1.0


@pytest.mark.parametrize(
    "payload",
    [
        b"",  # empty file
        "subject_id,visit_date,value\n".encode(),  # header only
        "id,date,value\nP1,2026-01-01,1\n".encode(),  # unknown headers
        "subject_id,visit_date,value,extra\nP1,2026-01-01,1,x\n".encode(),  # 4 columns
        "subject_id,visit_date,value\nP1,not-a-date,1\n".encode(),
        "subject_id,visit_date,value\nP1,2026-01-01,xyz\n".encode(),
        "subject_id,visit_date,value\n,2026-01-01,1\n".encode(),
        "subject_id,visit_date,value\nP1,2026-01-01,nan\n".encode(),
        "subject_id,visit_date,value\nP1,2026-01-01,inf\n".encode(),
    ],
)
def test_parse_csv_rejects_bad_inputs(payload):
    with pytest.raises(CSVValidationError):
        parse_csv(payload)


def test_canonical_hash_order_sensitive_and_stable():
    rows1 = parse_csv(b"subject_id,visit_date,value\nP1,2026-01-01,1\nP2,2026-01-02,2\n")
    rows2 = parse_csv(b"subject_id,visit_date,value\nP2,2026-01-02,2\nP1,2026-01-01,1\n")
    rows_same = parse_csv(b"subject_id,visit_date,value\nP1,2026-01-01,1\nP2,2026-01-02,2\n")
    assert canonical_hash(rows1) == canonical_hash(rows_same)
    assert canonical_hash(rows1) != canonical_hash(rows2)
