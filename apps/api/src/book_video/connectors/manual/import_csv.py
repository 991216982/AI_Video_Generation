"""CSV research-import parsing and row diagnostics for the manual connector.

This module is intentionally pure: it decodes, splits, sanitizes and validates CSV
cells into normalized draft records. It never writes business data, never executes
cell contents, and never fetches URLs. Persistence and owner scoping live in the
domain service. Contract: see `MetricObservation` / `ContentReference` / `ImportJob`
in `packages/contracts/openapi.yaml`.
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_ROWS = 500
MAX_FIELD_LENGTH = {
    "book.isbn": 20,
    "book.title": 200,
    "book.edition_label": 100,
    "book.author": 200,
    "book.publisher": 200,
    "book.original_opinion": 1000,
    "candidate.platform_product_id": 200,
    "candidate.product_url": 2000,
    "reference.external_ref": 200,
    "reference.reference_url": 2000,
    "reference.title": 200,
    "reference.note": 1000,
    "reference.license_scope": 40,
    "reference.usage_basis": 500,
    "metric.object_type": 40,
    "metric.object_ref": 200,
    "metric.object_label": 200,
    "metric.platform": 20,
    "metric.metric_name": 40,
    "metric.metric_definition": 500,
    "metric.basis": 40,
    "metric.unit": 40,
    "metric.value_text": 100,
    "metric.window_start": 40,
    "metric.window_end": 40,
    "metric.observed_at": 40,
    "metric.missing_reason": 500,
    "evidence.source_type": 20,
    "evidence.source_note": 1000,
    "evidence.source_url": 2000,
    "evidence.observed_at": 40,
    "evidence.usage_basis": 500,
}

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
PLATFORMS = {"douyin", "kuaishou"}
LICENSE_SCOPES = {"own_account", "licensed_data_service", "manual_observation"}
METRIC_BASES = {
    "authorized_order_sales",
    "platform_listing_display",
    "third_party_or_manual_estimate",
    "engagement_signal",
    "legacy_unclassified",
}
METRIC_NAMES = {
    "sales_units", "sales_amount", "listed_price", "displayed_sales_label", "plays",
    "likes", "comments", "shares", "favorites", "follower_count", "ranking_position",
    "search_heat", "other",
}
METRIC_UNITS = {
    "count", "minor_currency", "currency_yuan", "views", "likes", "percent",
    "rank", "score", "text", "unspecified",
}
OBJECT_TYPES = {"product_listing", "creator_account", "content_item", "book_edition"}

HEADER_SUGGESTIONS: dict[str, str] = {
    "isbn": "book.isbn",
    "title": "book.title",
    "book_title": "book.title",
    "edition": "book.edition_label",
    "edition_label": "book.edition_label",
    "version": "book.edition_label",
    "author": "book.author",
    "publisher": "book.publisher",
    "platform": "candidate.platform",
    "platform_product_id": "candidate.platform_product_id",
    "product_id": "candidate.platform_product_id",
    "product_url": "candidate.product_url",
    "url": "reference.reference_url",
    "metric_name": "metric.metric_name",
    "metric": "metric.metric_name",
    "basis": "metric.basis",
    "value": "metric.value",
    "observed_at": "metric.observed_at",
    "window_start": "metric.window_start",
    "window_end": "metric.window_end",
    "note": "reference.note",
}


def _parse_timestamp(value: str) -> datetime | None:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def normalize_isbn(value: str | None) -> str | None:
    if value is None:
        return None
    digits = re.sub(r"[^0-9Xx]", "", value).upper()
    if not digits:
        return None
    if len(digits) in (10, 13):
        return digits
    return None


def looks_like_formula(cell: str) -> bool:
    stripped = cell.lstrip()
    return stripped.startswith(FORMULA_PREFIXES) and len(stripped) > 1


def sanitize_cell(cell: str) -> str:
    if looks_like_formula(cell):
        return "'" + cell
    return cell


@dataclass
class RowDiagnostic:
    row_number: int
    code: str
    message: str
    severity: str = "error"
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        body = {"row_number": self.row_number, "code": self.code,
                "message": self.message, "severity": self.severity}
        if self.field is not None:
            body["field"] = self.field
        return body


@dataclass
class ParsedCsv:
    encoding_detected: str
    size_bytes: int
    file_sha256: str
    columns: list[str]
    rows: list[dict[str, str]] = field(default_factory=list)
    parse_diagnostics: list[RowDiagnostic] = field(default_factory=list)
    encoding_failed: bool = False


def parse_csv(content: bytes, *, sha256: str) -> ParsedCsv:
    size = len(content)
    if size == 0:
        return ParsedCsv("unknown", size, sha256, [],
                         parse_diagnostics=[RowDiagnostic(0, "encoding_error", "CSV file is empty", "error")])
    encoding_detected = "utf8"
    body = content
    if body.startswith(b"\xef\xbb\xbf"):
        encoding_detected = "utf8_bom"
        body = body[3:]
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return ParsedCsv("unknown", size, sha256, [],
                         parse_diagnostics=[RowDiagnostic(0, "encoding_error", "File is not valid UTF-8", "error")],
                         encoding_failed=True)

    reader = csv.reader(io.StringIO(text, newline=""))
    records = list(reader)
    diagnostics: list[RowDiagnostic] = []
    if not records:
        return ParsedCsv(encoding_detected, size, sha256, [],
                         parse_diagnostics=[RowDiagnostic(0, "malformed_row", "CSV has no header row", "error")],
                         encoding_failed=True)
    header = [column.strip() for column in records[0]]
    rows: list[dict[str, str]] = []
    for index, record in enumerate(records[1:], start=1):
        if not any(cell.strip() for cell in record):
            continue
        if len(record) != len(header):
            diagnostics.append(RowDiagnostic(index, "malformed_row", "Row column count does not match header"))
            continue
        rows.append({header[i]: record[i] for i in range(len(header))})
    if len(rows) > MAX_ROWS:
        diagnostics.append(RowDiagnostic(0, "malformed_row", f"Row count exceeds the {MAX_ROWS}-row limit"))
    return ParsedCsv(encoding_detected, size, sha256, header, rows, diagnostics)


def suggest_mapping(columns: Iterable[str]) -> dict[str, str | None]:
    suggestions: dict[str, str | None] = {}
    for column in columns:
        key = column.strip().lower().replace("-", "_")
        suggestions[column] = HEADER_SUGGESTIONS.get(key)
    return suggestions


VALID_MAPPING_TARGETS = set(HEADER_SUGGESTIONS.values()) | {
    "book.original_opinion",
    "reference.ref_type",
    "reference.external_ref",
    "metric.object_type",
    "metric.object_ref",
    "metric.object_label",
    "metric.platform",
    "metric.unit",
    "metric.value_text",
    "metric.metric_definition",
    "metric.book_id",
    "evidence.source_type",
    "evidence.source_note",
    "evidence.source_url",
    "evidence.usage_basis",
}


def _cell(mapping: dict[str, str | None], row: dict[str, str], target: str) -> str:
    for source, mapped in mapping.items():
        if mapped == target:
            raw = row.get(source, "")
            return sanitize_cell(raw).strip()
    return ""


def validate_rows(
    rows: list[dict[str, str]],
    mapping: dict[str, str | None],
    *,
    match_by: str = "isbn_and_edition",
    do_normalize_isbn: bool = True,
) -> tuple[list[dict[str, Any]], list[RowDiagnostic], dict[str, int]]:
    """Validate mapped rows into normalized draft records.

    Returns (records, diagnostics, counts). Records with error diagnostics are still
    returned so the preview can surface them, but callers must not persist them.
    """
    diagnostics: list[RowDiagnostic] = []
    records: list[dict[str, Any]] = []
    seen_keys: dict[str, str] = {}
    title_author_groups: dict[tuple[str, str], list[str]] = {}

    for row_number, row in enumerate(rows, start=1):
        row_errors: list[RowDiagnostic] = []
        values: dict[str, str] = {}

        def require(target: str, field: str) -> str:
            value = _cell(mapping, row, target)
            values[field] = value
            return value

        raw_isbn = _cell(mapping, row, "book.isbn")
        isbn = normalize_isbn(raw_isbn) if do_normalize_isbn else raw_isbn
        title = require("book.title", "title")
        edition = _cell(mapping, row, "book.edition_label")
        author = _cell(mapping, row, "book.author")
        platform = _cell(mapping, row, "candidate.platform") or _cell(mapping, row, "metric.platform")
        product_id = _cell(mapping, row, "candidate.platform_product_id")
        product_url = _cell(mapping, row, "candidate.product_url")
        ref_type = _cell(mapping, row, "reference.ref_type")
        metric_name = _cell(mapping, row, "metric.metric_name")
        basis = _cell(mapping, row, "metric.basis")
        unit = _cell(mapping, row, "metric.unit") or "unspecified"
        raw_value = _cell(mapping, row, "metric.value")
        value_text = _cell(mapping, row, "metric.value_text")
        observed_at = _cell(mapping, row, "metric.observed_at") or _cell(mapping, row, "evidence.observed_at")
        window_start = _cell(mapping, row, "metric.window_start")
        window_end = _cell(mapping, row, "metric.window_end")
        missing_reason = _cell(mapping, row, "metric.missing_reason")

        # Formula injection: any mapped cell that still looks like a raw formula.
        for source, mapped in mapping.items():
            if mapped and looks_like_formula(row.get(source, "")):
                row_errors.append(RowDiagnostic(row_number, "formula_injection",
                                                "Cell starts with a formula prefix; treated as text and rejected",
                                                "error", source))

        # Platform validation.
        if platform and platform not in PLATFORMS:
            row_errors.append(RowDiagnostic(row_number, "invalid_platform",
                                             f"Unknown platform '{platform}'", "error", "platform"))

        # URL validation.
        for url_field, url in (("product_url", product_url), ("reference_url", _cell(mapping, row, "reference.reference_url"))):
            if url and not re.match(r"^https?://", url):
                row_errors.append(RowDiagnostic(row_number, "invalid_url",
                                                "URL must be an http(s) link", "error", url_field))

        # ISBN shape.
        if isbn and re.sub(r"[^0-9Xx]", "", isbn) and len(re.sub(r"[^0-9Xx]", "", isbn)) not in (10, 13):
            row_errors.append(RowDiagnostic(row_number, "invalid_book_isbn", "ISBN is not 10 or 13 digits",
                                            "error", "book.isbn"))

        # Numeric value parsing.
        numeric_value: float | None = None
        if raw_value:
            try:
                parsed = float(raw_value.replace(",", ""))
                if not math.isfinite(parsed):
                    raise ValueError("non-finite")
                numeric_value = parsed
            except ValueError:
                row_errors.append(RowDiagnostic(row_number, "invalid_number",
                                                 "Metric value is not a finite number", "error", "metric.value"))
        elif missing_reason:
            numeric_value = None
        else:
            numeric_value = None

        # Timestamps.
        observed_dt = _parse_timestamp(observed_at) if observed_at else None
        if observed_at and observed_dt is None:
            row_errors.append(RowDiagnostic(row_number, "invalid_timestamp",
                                             "Observed_at is not a timezone-aware RFC 3339 timestamp",
                                             "error", "metric.observed_at"))
        window_start_dt = _parse_timestamp(window_start) if window_start else None
        if window_start and window_start_dt is None:
            row_errors.append(RowDiagnostic(row_number, "invalid_timestamp", "Window start is invalid",
                                            "error", "metric.window_start"))
        window_end_dt = _parse_timestamp(window_end) if window_end else None
        if window_end and window_end_dt is None:
            row_errors.append(RowDiagnostic(row_number, "invalid_timestamp", "Window end is invalid",
                                            "error", "metric.window_end"))
        if window_start_dt and window_end_dt and window_start_dt > window_end_dt:
            row_errors.append(RowDiagnostic(row_number, "invalid_timestamp", "Window start follows window end",
                                            "error", "metric.window_start"))

        # Enumerations.
        if basis and basis not in METRIC_BASES:
            row_errors.append(RowDiagnostic(row_number, "invalid_mapping", f"Unknown metric basis '{basis}'",
                                            "error", "metric.basis"))
        if metric_name and metric_name not in METRIC_NAMES:
            row_errors.append(RowDiagnostic(row_number, "invalid_mapping", f"Unknown metric name '{metric_name}'",
                                            "error", "metric.metric_name"))
        if unit and unit not in METRIC_UNITS:
            row_errors.append(RowDiagnostic(row_number, "invalid_mapping", f"Unknown metric unit '{unit}'",
                                            "error", "metric.unit"))
        if ref_type and ref_type not in OBJECT_TYPES:
            row_errors.append(RowDiagnostic(row_number, "invalid_mapping", f"Unknown reference type '{ref_type}'",
                                            "error", "reference.ref_type"))

        # Required fields for the entities this row can describe.
        has_book = bool(title or isbn)
        has_candidate = bool(product_id or product_url)
        has_metric = bool(metric_name or basis or raw_value or value_text)
        has_reference = bool(_cell(mapping, row, "reference.title") or _cell(mapping, row, "reference.reference_url"))
        if not (has_book or has_candidate or has_metric or has_reference):
            row_errors.append(RowDiagnostic(row_number, "missing_required_field",
                                            "Row does not map to any known entity field", "error"))

        # Natural key and duplicate / edition detection.
        key_parts: list[str] = []
        if match_by == "title_author_fuzzy" or (not isbn):
            key_parts.extend([title.casefold(), author.casefold(), edition.casefold()])
        else:
            if isbn:
                key_parts.append(f"isbn:{isbn}")
            if match_by == "isbn_and_edition" and edition:
                key_parts.append(f"edition:{edition.casefold()}")
        natural_key = "|".join(part for part in key_parts if part) or f"row:{row_number}"
        if natural_key in seen_keys:
            row_errors.append(RowDiagnostic(row_number, "duplicate_natural_key",
                                           "Natural key duplicates an earlier row in this file", "warning", "isbn"))
        else:
            seen_keys[natural_key] = str(row_number)

        if title and author:
            group = (title.casefold(), author.casefold())
            group_value = f"{isbn or '-'}|{edition or '-'}"
            if group in title_author_groups and title_author_groups[group] != group_value:
                row_errors.append(RowDiagnostic(row_number, "edition_mismatch_suspected",
                                                 "Same title and author but a different ISBN/edition; not auto-merged",
                                                 "warning", "book.isbn"))
            title_author_groups.setdefault(group, group_value)

        # Length guards.
        for target, limit in MAX_FIELD_LENGTH.items():
            source = next((src for src, mapped in mapping.items() if mapped == target), None)
            if source and len(row.get(source, "")) > limit:
                row_errors.append(RowDiagnostic(row_number, "field_too_long",
                                                f"Field exceeds the {limit}-character limit", "error", source))

        diagnostics.extend(row_errors)
        records.append({
            "row_number": row_number,
            "values": values,
            "book": {
                "isbn": isbn, "title": title, "edition_label": edition, "author": author,
                "publisher": _cell(mapping, row, "book.publisher"),
                "original_opinion": _cell(mapping, row, "book.original_opinion"),
            },
            "candidate": {
                "platform": platform or None, "platform_product_id": product_id or None,
                "product_url": product_url or None,
            },
            "metric": {
                "object_type": ref_type or ("product_listing" if has_candidate else "book_edition"),
                "object_ref": product_id or _cell(mapping, row, "metric.object_ref") or (f"isbn:{isbn}" if isbn else None),
                "object_label": _cell(mapping, row, "metric.object_label") or title or None,
                "platform": platform or None,
                "metric_name": metric_name or "other",
                "metric_definition": _cell(mapping, row, "metric.metric_definition") or "CSV 导入指标，口径待人工核实",
                "basis": basis or "legacy_unclassified",
                "unit": unit,
                "value": numeric_value,
                "value_text": value_text or None,
                "window_start": window_start_dt.isoformat().replace("+00:00", "Z") if window_start_dt else None,
                "window_end": window_end_dt.isoformat().replace("+00:00", "Z") if window_end_dt else None,
                "observed_at": observed_dt.isoformat().replace("+00:00", "Z") if observed_dt else None,
                "missing_reason": missing_reason or None,
                "note": "CSV 导入记录",
            },
            "reference": {
                "ref_type": ref_type or None,
                "external_ref": _cell(mapping, row, "reference.external_ref") or product_id or None,
                "reference_url": _cell(mapping, row, "reference.reference_url") or product_url or None,
                "title": _cell(mapping, row, "reference.title") or title or None,
                "note": _cell(mapping, row, "reference.note") or None,
                "license_scope": _cell(mapping, row, "reference.license_scope") or "manual_observation",
                "usage_basis": _cell(mapping, row, "reference.usage_basis") or "人工观察记录，仅作研究参考",
            },
            "has_metric": has_metric,
            "has_candidate": has_candidate,
            "has_reference": has_reference,
            "error_count": sum(1 for d in row_errors if d.severity == "error"),
            "warning_count": sum(1 for d in row_errors if d.severity == "warning"),
        })

    counts = _counts(records, diagnostics)
    return records, diagnostics, counts


def _counts(records: list[dict[str, Any]], diagnostics: list[RowDiagnostic]) -> dict[str, int]:
    duplicate_rows = {d.row_number for d in diagnostics if d.code == "duplicate_natural_key"}
    failed_rows = {r["row_number"] for r in records if r["error_count"] > 0}
    skipped = sum(1 for r in records if r["row_number"] in duplicate_rows and r["row_number"] not in failed_rows)
    failed = len(failed_rows)
    return {
        "total": len(records),
        "parsed": len(records),
        "persisted": 0,
        "skipped_duplicate": skipped,
        "failed": failed,
        "warnings": sum(1 for d in diagnostics if d.severity == "warning"),
    }
