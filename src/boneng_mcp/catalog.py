"""Read-only queries against the catalog extracted from the release databases."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .paths import UserInputError, read_json


def bounded_page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= 100:
        raise UserInputError("limit must be between 1 and 100.")
    if isinstance(offset, bool) or not 0 <= offset <= 1_000_000:
        raise UserInputError("offset must be between 0 and 1000000.")


class Catalog:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise UserInputError("Catalog is missing. Run scripts/build_catalog.py first.")
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def list_catalogs(self) -> list[dict[str, Any]]:
        report_path = self.path.with_name("catalog_build_report.json")
        report = read_json(report_path) if report_path.is_file() else {"sources": []}
        reports = {source["id"]: source for source in report["sources"]}
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT * FROM datasets ORDER BY id").fetchall()
            return [
                {**dict(row), "columns": json.loads(row["columns"]), "reference_only": True,
                 "extraction_warnings": reports.get(row["id"], {}).get("errors", []),
                 "ambiguous_codes": reports.get(row["id"], {}).get("duplicate_codes", [])}
                for row in rows
            ]

    def dataset(self, dataset_id: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if row is None:
            raise UserInputError("Unknown catalog ID. Call list_parameter_catalogs first.")
        return {**dict(row), "columns": json.loads(row["columns"])}

    def search(self, query: str, dataset_id: str = "", limit: int = 20, offset: int = 0) -> dict[str, Any]:
        bounded_page(limit, offset)
        if not query.strip() or len(query) > 200:
            raise UserInputError("query must contain 1–200 characters.")
        if dataset_id:
            self.dataset(dataset_id)
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where = "search_text LIKE ? ESCAPE '\\'"
        parameters: list[Any] = [f"%{escaped}%"]
        if dataset_id:
            where += " AND dataset_id=?"
            parameters.append(dataset_id)
        with closing(self.connect()) as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM records WHERE {where}", parameters).fetchone()[0]
            rows = connection.execute(
                f"SELECT dataset_id,source_table,code,name,raw_json FROM records WHERE {where} "
                "ORDER BY dataset_id,row_number LIMIT ? OFFSET ?", parameters + [limit, offset]
            ).fetchall()
        return {
            "total": total, "offset": offset, "limit": limit,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
            "reference_only": True,
            "items": [self._record(row) for row in rows],
        }

    def definition(self, dataset_id: str, code: str) -> dict[str, Any]:
        dataset = self.dataset(dataset_id)
        if not dataset["code_column"]:
            raise UserInputError("This reference table has no recognized parameter code column.")
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT dataset_id,source_table,code,name,raw_json FROM records WHERE dataset_id=? AND code=?",
                (dataset_id, code),
            ).fetchall()
        if not rows:
            raise UserInputError(f"Parameter code not found in selected catalog: {code}")
        if len(rows) != 1:
            raise UserInputError(f"Parameter code is ambiguous in selected catalog: {code}")
        return {**self._record(rows[0]), "source": dataset["source"]}

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "catalog_id": row["dataset_id"], "source_table": row["source_table"], "code": row["code"], "name": row["name"],
            "fields": json.loads(row["raw_json"]), "reference_only": True,
            "device_address_verified": False,
        }
