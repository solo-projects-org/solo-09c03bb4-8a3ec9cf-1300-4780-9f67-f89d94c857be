"""CSV parsing, validation and canonical hashing of uploaded revisions.

The accepted layout is one header row plus data rows with exactly three
columns. Bilingual headers are accepted so the data team can hand over either
an English or a Chinese template:

    subject_id, visit_date, value
    受试者号, 访视日期, 测值
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
from dataclasses import dataclass
from datetime import date

HEADER_ALIASES = {
    "subject_id": {"subject_id", "subjectid", "受试者号", "受试者编号"},
    "visit_date": {"visit_date", "visitdate", "访视日期", "访视时间"},
    "value": {"value", "measurement", "measure", "测值", "测量值"},
}


class CSVValidationError(ValueError):
    """Raised when an uploaded file is not a complete, well-formed revision."""


@dataclass(frozen=True)
class Row:
    subject_id: str
    visit_date: date
    value: float


def _normalise_header(name: str) -> str:
    return name.strip().lstrip("﻿").lower().replace(" ", "_")


def _map_headers(fieldnames: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, raw in enumerate(fieldnames):
        key = _normalise_header(raw or "")
        for canonical, aliases in HEADER_ALIASES.items():
            if key in aliases:
                if canonical in mapping:
                    raise CSVValidationError(
                        f"表头存在重复列: {raw!r} 与 {fieldnames[mapping[canonical]]!r}"
                    )
                mapping[canonical] = idx
    missing = [c for c in ("subject_id", "visit_date", "value") if c not in mapping]
    if missing:
        raise CSVValidationError(
            "缺少必需列: " + ", ".join(missing)
            + f"；实际表头: {fieldnames}"
        )
    if len(fieldnames) != 3:
        raise CSVValidationError(f"CSV 必须恰好包含 3 列，实际 {len(fieldnames)} 列: {fieldnames}")
    return mapping


def parse_csv(content: bytes) -> list[Row]:
    """Parse and validate a complete revision payload."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CSVValidationError("文件必须是 UTF-8 编码的 CSV") from exc

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise CSVValidationError("CSV 为空，缺少表头")

    mapping = _map_headers(header)
    rows: list[Row] = []
    for line_no, raw_row in enumerate(reader, start=2):
        if not raw_row or all((cell or "").strip() == "" for cell in raw_row):
            continue  # tolerate trailing blank lines
        if len(raw_row) != 3:
            raise CSVValidationError(f"第 {line_no} 行列数为 {len(raw_row)}，应为 3")

        subject_id = raw_row[mapping["subject_id"]].strip()
        date_text = raw_row[mapping["visit_date"]].strip()
        value_text = raw_row[mapping["value"]].strip()

        if not subject_id:
            raise CSVValidationError(f"第 {line_no} 行受试者号为空")
        if not date_text:
            raise CSVValidationError(f"第 {line_no} 行访视日期为空")
        if not value_text:
            raise CSVValidationError(f"第 {line_no} 行测值为空")

        try:
            visit_date = date.fromisoformat(date_text)
        except ValueError as exc:
            raise CSVValidationError(
                f"第 {line_no} 行日期格式无效 (需 YYYY-MM-DD): {date_text!r}"
            ) from exc
        try:
            value = float(value_text)
        except ValueError as exc:
            raise CSVValidationError(f"第 {line_no} 行测值不是数字: {value_text!r}") from exc
        if not math.isfinite(value):
            raise CSVValidationError(f"第 {line_no} 行测值必须是有限数字: {value_text!r}")

        rows.append(Row(subject_id=subject_id, visit_date=visit_date, value=value))

    if not rows:
        raise CSVValidationError("CSV 不包含任何数据行，不能作为完整修订受理")
    return rows


def canonical_hash(rows: list[Row]) -> str:
    """Deterministic content hash over the validated rows.

    Rows are serialised in their original order so re-uploads of the exact same
    file are idempotent, while any content change produces a different hash and
    is rejected as a conflicting revision.
    """
    h = hashlib.sha256()
    for row in rows:
        h.update(
            f"{row.subject_id}\t{row.visit_date.isoformat()}\t{row.value!r}\n".encode("utf-8")
        )
    return h.hexdigest()
