"""Numerical summaries for existing CSV recordings, without live acquisition."""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

from .paths import UserInputError, confined_file


def summarize_waveform(workspace: Path, filename: str, time_column: str = "time_s") -> dict[str, Any]:
    path = confined_file(workspace, filename, ".csv")
    if not path.is_file():
        raise UserInputError("CSV file not found inside the MCP workspace.")
    if path.stat().st_size > 20_000_000:
        raise UserInputError("Waveform CSV is limited to 20 MB.")
    stats: dict[str, dict[str, Any]] = {}
    first_time = last_time = None
    minimum_step, maximum_step = math.inf, -math.inf
    row_count = 0
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames
            if not fields or len(set(fields)) != len(fields) or len(fields) > 65 or any(not name for name in fields):
                raise UserInputError("CSV must have unique non-empty column names, with at most 64 channels plus time.")
            if time_column not in fields or len(fields) < 2:
                raise UserInputError("CSV needs the specified time column in seconds and at least one signal column.")
            channels = [field for field in fields if field != time_column]
            for index, row in enumerate(reader, start=2):
                if index > 200_001:
                    raise UserInputError("CSV is limited to 200000 samples.")
                if None in row or any(value is None or not value.strip() for value in row.values()):
                    raise UserInputError(f"Missing or extra CSV fields on row {index}.")
                try:
                    values = {name: float(text) for name, text in row.items()}
                except ValueError as error:
                    raise UserInputError(f"Non-numeric waveform value on row {index}.") from error
                if not all(math.isfinite(value) and abs(value) <= 1e100 for value in values.values()):
                    raise UserInputError(f"Invalid or excessively large waveform value on row {index}.")
                time = values[time_column]
                if last_time is not None:
                    step = time - last_time
                    if step <= 0:
                        raise UserInputError("Time values must be strictly increasing and expressed in seconds.")
                    minimum_step, maximum_step = min(minimum_step, step), max(maximum_step, step)
                else:
                    first_time = time
                last_time = time
                row_count += 1
                for name in channels:
                    value = values[name]
                    stat = stats.setdefault(name, {"minimum": value, "maximum": value, "mean": 0.0, "mean_square": 0.0})
                    stat["minimum"], stat["maximum"] = min(stat["minimum"], value), max(stat["maximum"], value)
                    stat["mean"] += (value - stat["mean"]) / row_count
                    stat["mean_square"] += (value * value - stat["mean_square"]) / row_count
    except (UnicodeError, csv.Error) as error:
        raise UserInputError("Cannot read CSV; use UTF-8, comma delimiters and numeric columns.") from error
    if not row_count:
        raise UserInputError("CSV has no samples.")
    for stat in stats.values():
        stat["rms"] = math.sqrt(max(0, stat.pop("mean_square")))
        stat["peak_to_peak"] = stat["maximum"] - stat["minimum"]
    return {
        "filename": filename, "origin": "existing_csv_file", "live_capture": False,
        "samples": row_count, "time_unit": "seconds (required input convention)",
        "duration_s": last_time - first_time, "signals": stats,
        "sample_step_min_s": minimum_step if row_count > 1 else None,
        "sample_step_max_s": maximum_step if row_count > 1 else None,
    }
