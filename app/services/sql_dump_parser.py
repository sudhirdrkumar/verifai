from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Iterator


INSERT_RE = re.compile(
    r"^\s*INSERT\s+INTO\s+`?([a-zA-Z0-9_]+)`?\s*\((.*?)\)\s*VALUES\s*(.*)\s*;\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _iter_insert_statements(lines: Iterable[str]) -> Iterator[str]:
    buffer: list[str] = []
    capturing = False

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        stripped = line.lstrip()
        upper = stripped.upper()

        if not capturing:
            if upper.startswith("INSERT INTO "):
                capturing = True
                buffer = [line]
                if stripped.rstrip().endswith(";"):
                    yield "\n".join(buffer)
                    buffer = []
                    capturing = False
            continue

        buffer.append(line)
        if stripped.rstrip().endswith(";"):
            yield "\n".join(buffer)
            buffer = []
            capturing = False


def _parse_sql_string(blob: str, start: int) -> tuple[str, int]:
    out: list[str] = []
    i = start + 1  # skip opening quote
    n = len(blob)
    escape_map = {
        "0": "\0",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "Z": "\x1a",
        "\\": "\\",
        "'": "'",
        '"': '"',
    }

    while i < n:
        ch = blob[i]
        if ch == "\\" and i + 1 < n:
            esc = blob[i + 1]
            out.append(escape_map.get(esc, esc))
            i += 2
            continue
        if ch == "'":
            if i + 1 < n and blob[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(ch)
        i += 1

    return "".join(out), i


def _parse_unquoted_token(token: str) -> Any:
    value = token.strip()
    if value == "":
        return ""

    upper = value.upper()
    if upper == "NULL":
        return None
    if upper == "TRUE":
        return True
    if upper == "FALSE":
        return False

    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except Exception:
            return value

    if re.fullmatch(r"-?\d+\.\d+", value):
        try:
            return float(value)
        except Exception:
            return value

    return value


def _iter_insert_values_rows(values_blob: str) -> Iterator[list[Any]]:
    i = 0
    n = len(values_blob)

    while i < n:
        while i < n and values_blob[i] in {" ", "\t", "\r", "\n", ","}:
            i += 1
        if i >= n:
            break
        if values_blob[i] != "(":
            i += 1
            continue

        i += 1
        row: list[Any] = []

        while i < n:
            while i < n and values_blob[i] in {" ", "\t", "\r", "\n"}:
                i += 1
            if i >= n:
                break

            ch = values_blob[i]
            if ch == "'":
                parsed, i = _parse_sql_string(values_blob, i)
                row.append(parsed)
            else:
                start = i
                while i < n and values_blob[i] not in {",", ")"}:
                    i += 1
                row.append(_parse_unquoted_token(values_blob[start:i]))

            while i < n and values_blob[i] in {" ", "\t", "\r", "\n"}:
                i += 1

            if i < n and values_blob[i] == ",":
                i += 1
                continue
            if i < n and values_blob[i] == ")":
                i += 1
                yield row
                break


def _parse_insert_statement(statement: str) -> tuple[str, list[str], str] | None:
    match = INSERT_RE.match(statement)
    if not match:
        return None

    table = str(match.group(1) or "").strip()
    columns_blob = str(match.group(2) or "")
    values_blob = str(match.group(3) or "")
    if not table:
        return None

    columns = [str(part).strip().strip("`").strip() for part in columns_blob.split(",")]
    columns = [col for col in columns if col]
    if not columns:
        return None

    return table, columns, values_blob


def _iter_rows_from_lines(lines: Iterable[str], table_name: str) -> Iterator[dict[str, Any]]:
    target = str(table_name or "").strip().lower()
    if not target:
        return

    for statement in _iter_insert_statements(lines):
        parsed = _parse_insert_statement(statement)
        if parsed is None:
            continue

        table, columns, values_blob = parsed
        if table.strip().lower() != target:
            continue

        width = len(columns)
        for values in _iter_insert_values_rows(values_blob):
            if not values:
                continue
            if len(values) < width:
                values = values + [None] * (width - len(values))
            elif len(values) > width:
                values = values[:width]
            yield dict(zip(columns, values))


def iter_table_rows_from_sql_dump_path(path: str | Path, table_name: str) -> Iterator[dict[str, Any]]:
    sql_path = Path(path)
    with sql_path.open("r", encoding="utf-8-sig", errors="ignore") as handle:
        yield from _iter_rows_from_lines(handle, table_name)


def iter_table_rows_from_sql_dump_bytes(payload: bytes, table_name: str) -> Iterator[dict[str, Any]]:
    text_payload = payload.decode("utf-8-sig", errors="ignore")
    yield from _iter_rows_from_lines(text_payload.splitlines(), table_name)

