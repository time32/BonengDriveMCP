"""Extract reference parameters without requiring an Access/Office installation.

access-parser 0.0.6 enumerates dynamic fields in display order. In this release,
several ACE tables moved ID to the first display position while its stored slot
remained last. Reorder by variable_column_number before calling its parser.
This is an adapter around the library, not a modification of the release files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path
from types import MethodType

PROJECT = Path(__file__).resolve().parents[1]
if (PROJECT / ".analysis-deps").is_dir():
    sys.path.insert(0, str(PROJECT / ".analysis-deps"))

from access_parser import AccessParser
from access_parser.access_parser import AccessTable

CODE = re.compile(r"[A-Z][0-9]{2}\.[0-9]{2,3}\Z")


def parse_in_storage_order(self, record, metadata, mapping, null_table):
    ordered = dict(sorted(mapping.items(), key=lambda item: item[1].variable_column_number))
    slots = [column.variable_column_number for column in ordered.values()]
    if slots != list(range(len(ordered))):
        raise ValueError("Unsupported non-contiguous ACE variable-column slots; table excluded.")
    return AccessTable._parse_dynamic_length_data(self, record, metadata, ordered, null_table)


def extract_table(database, name):
    table = database.get_table(name)
    if table.version <= 3:
        raise ValueError("Only the supplied ACE database format is supported by this extractor.")
    table._parse_dynamic_length_data = MethodType(parse_in_storage_order, table)
    data = table.parse()
    if not data or any(not isinstance(values, list) for values in data.values()):
        raise ValueError("Table did not parse into column arrays.")
    lengths = {len(values) for values in data.values()}
    if len(lengths) != 1:
        raise ValueError("Column lengths disagree; no partial rows will be published.")
    return data


def clean_scalar(value):
    if isinstance(value, bytes):
        raise ValueError("Unparsed binary field; table excluded.")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite field; table excluded.")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def extract_database(path: Path, dataset_id: str):
    database = AccessParser(str(path))
    records, tables, errors = [], [], []
    for name in database.catalog:
        if name.startswith(("MSys", "f_", "~")):
            continue
        try:
            data = extract_table(database, name)
            if "功能码" not in data or "名称" not in data:
                tables.append({"table": name, "role": "reference_not_indexed"})
                continue
            table_records = []
            for index, raw_code in enumerate(data["功能码"]):
                if not isinstance(raw_code, str) or not CODE.fullmatch(raw_code.strip()):
                    continue  # Group headings are not parameter codes.
                raw = {column: clean_scalar(values[index]) for column, values in data.items()}
                code, name_value = raw_code.strip(), raw["名称"]
                if not isinstance(name_value, str) or not name_value.strip():
                    raise ValueError(f"Missing parameter name for {code}.")
                address = raw.get("通讯地址")
                if address is not None and address != "" and not re.fullmatch(r"0[xX][0-9a-fA-F]+", str(address)):
                    raise ValueError(f"Misaligned address field for {code}; table excluded.")
                raw_json = json.dumps(raw, ensure_ascii=False, allow_nan=False)
                text = "\n".join(str(value) for value in raw.values() if value is not None)
                table_records.append((dataset_id, name, index + 1, code, name_value, text, raw_json))
            records.extend(table_records)
            tables.append({"table": name, "source_rows": len(data["功能码"]), "parameter_count": len(table_records)})
        except Exception as error:
            errors.append({"table": name, "error_type": type(error).__name__, "message": str(error)[:300]})
    duplicates = [code for code, count in Counter(row[3] for row in records).items() if count > 1]
    return records, {
        "id": dataset_id, "source": f"AccessDatabase/{path.name}",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "tables": tables, "errors": errors, "duplicate_codes": duplicates,
        "parameter_count": len(records), "device_model_verified": False,
    }


def build(release: Path, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    sources = sorted((release / "AccessDatabase").glob("*1.11.mdb"))
    if not sources:
        raise ValueError("No V1.11 source databases found. Explicitly review new database versions before extraction.")
    destination = output / "catalog.sqlite3"
    temporary = output / "catalog.building.sqlite3"
    if temporary.exists():
        temporary.unlink()
    report = {"schema_version": 1, "parser": "access-parser 0.0.6 + validated storage-slot ordering", "sources": []}
    try:
        with closing(sqlite3.connect(temporary)) as connection, connection:
            connection.executescript("""
                CREATE TABLE datasets(id TEXT PRIMARY KEY, source TEXT NOT NULL, source_table TEXT NOT NULL,
                    row_count INTEGER NOT NULL, code_column TEXT NOT NULL, columns TEXT NOT NULL);
                CREATE TABLE records(dataset_id TEXT NOT NULL, source_table TEXT NOT NULL, row_number INTEGER NOT NULL,
                    code TEXT NOT NULL, name TEXT NOT NULL, search_text TEXT NOT NULL, raw_json TEXT NOT NULL);
                CREATE INDEX records_code ON records(dataset_id,code);
            """)
            for path in sources:
                language = "en" if path.name.startswith("MINI Function") else "cn"
                dataset_id = language + "-v1.11"
                records, source = extract_database(path, dataset_id)
                if not records:
                    raise ValueError(f"No validated parameters extracted from {path.name}.")
                columns = sorted({key for row in records for key in json.loads(row[6])})
                connection.execute("INSERT INTO datasets VALUES (?,?,?,?,?,?)", (
                    dataset_id, source["source"], "multiple_parameter_groups", len(records), "功能码", json.dumps(columns, ensure_ascii=False),
                ))
                connection.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?)", records)
                report["sources"].append(source)
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed.")
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    (output / "catalog_build_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, default=PROJECT.parent)
    parser.add_argument("--output-dir", type=Path, default=PROJECT / "data")
    args = parser.parse_args()
    logging.disable(logging.WARNING)
    result = build(args.release_dir.resolve(), args.output_dir.resolve())
    print(json.dumps({"catalogs": len(result["sources"]), "parameters": sum(source["parameter_count"] for source in result["sources"]), "excluded_tables": sum(len(source["errors"]) for source in result["sources"])}, ensure_ascii=True))
