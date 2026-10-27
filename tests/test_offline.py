from __future__ import annotations

import json
import math
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from boneng_mcp.catalog import Catalog
from boneng_mcp.parameter_sets import ParameterSets
from boneng_mcp.paths import UserInputError, read_json
from boneng_mcp.waveforms import summarize_waveform


class OfflineTests(unittest.TestCase):
    def setUp(self):
        parent = (PROJECT / "workspace" / "test_artifacts").resolve()
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="unit_", dir=parent)
        self.root = Path(self.temporary.name).resolve()
        assert self.root.is_relative_to(parent)
        path = self.root / "catalog.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.executescript("""
                CREATE TABLE datasets(id TEXT, source TEXT, source_table TEXT, row_count INTEGER, code_column TEXT, columns TEXT);
                CREATE TABLE records(dataset_id TEXT, source_table TEXT, row_number INTEGER, code TEXT, name TEXT, search_text TEXT, raw_json TEXT);
            """)
            for dataset in ["cn", "en"]:
                connection.execute("INSERT INTO datasets VALUES (?,?,?,?,?,?)", (dataset, "source.mdb", "multiple", 4, "功能码", "[]"))
                for index, (code, name) in enumerate([("A00.00", "状态"), ("D00.00", "电机类型"), ("F01.36", "重复1"), ("F01.36", "重复2")]):
                    raw = {"名称": name, "设定范围": "0~100%", "修改级别（2bit）": "只读参数" if code.startswith("A") else "停机修改"}
                    connection.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?)", (dataset, code[0] + "组", index, code, name, name + " 0~100%", json.dumps(raw, ensure_ascii=False)))
        connection.close()
        self.catalog = Catalog(path)
        self.sets = ParameterSets(self.root, self.catalog)

    def tearDown(self):
        self.temporary.cleanup()

    def save(self, name, values, catalog="cn"):
        return self.sets.save(name, catalog, values)

    def csv(self, text):
        (self.root / "wave.csv").write_text(text, encoding="utf-8")
        return summarize_waveform(self.root, "wave.csv")

    def test_source_provenance_is_preserved(self):
        result = self.catalog.definition("cn", "D00.00")
        self.assertEqual(result["source_table"], "D组")
        self.assertFalse(result["device_address_verified"])

    def test_literal_search_does_not_expand_wildcards(self):
        self.assertEqual(self.catalog.search("_")["total"], 0)
        self.assertEqual(self.catalog.search("100%", "cn")["total"], 4)
        self.assertEqual(self.catalog.search("' OR 1=1 --")["total"], 0)

    def test_search_pagination_has_no_overlap(self):
        first = self.catalog.search("100%", "cn", limit=2)
        second = self.catalog.search("100%", "cn", limit=2, offset=first["next_offset"])
        self.assertEqual([item["code"] for item in first["items"]], ["A00.00", "D00.00"])
        self.assertIsNone(second["next_offset"])

    def test_ambiguous_reference_is_not_chosen_arbitrarily(self):
        with self.assertRaisesRegex(UserInputError, "ambiguous"):
            self.save("ambiguous", {"F01.36": "0"})

    def test_parameter_files_do_not_overwrite_existing_data(self):
        self.save("base", {"D00.00": "0"})
        before = self.sets._path("base").read_bytes()
        with self.assertRaises(UserInputError):
            self.save("base", {"D00.00": "1"})
        self.assertEqual(before, self.sets._path("base").read_bytes())

    def test_file_removal_never_becomes_device_reset(self):
        self.save("base", {"D00.00": "0", "A00.00": "0"})
        self.save("target", {"D00.00": "1"})
        plan = self.sets.plan("base", "target")
        self.assertEqual([entry["code"] for entry in plan["operations"]], ["D00.00"])
        self.assertFalse(plan["execution_supported"])
        self.assertEqual(plan["omitted_parameters_action"], "leave_unchanged")

    def test_plan_exposes_read_only_reference(self):
        self.save("base", {"A00.00": "0"})
        self.save("target", {"A00.00": "1"})
        self.assertTrue(self.sets.plan("base", "target")["operations"][0]["catalog_read_only"])

    def test_decimal_formatting_does_not_produce_false_changes(self):
        self.save("base", {"D00.00": "1.000"})
        self.save("target", {"D00.00": "1"})
        self.assertEqual(self.sets.compare("base", "target")["total_changes"], 0)

    def test_different_catalogs_cannot_be_applied_as_same_model(self):
        self.save("base", {"D00.00": "0"})
        self.save("target", {"D00.00": "1"}, "en")
        with self.assertRaises(UserInputError):
            self.sets.plan("base", "target")

    def test_boolean_and_non_finite_values_are_rejected(self):
        for value in [True, float("inf"), float("nan"), "NaN", "Infinity", "sNaN"]:
            with self.subTest(value=value), self.assertRaises(UserInputError):
                self.save("invalid", {"D00.00": value})

    def test_ids_cannot_read_outside_the_parameter_directory(self):
        for name in ["../source", "..\\source", "/absolute", "C:\\source", "file:stream"]:
            with self.subTest(name=name), self.assertRaises(UserInputError):
                self.sets.load(name)

    def test_user_files_cannot_claim_a_live_reading(self):
        self.save("base", {"D00.00": "0"})
        path = self.sets._path("base")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["device_verified"] = True
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(UserInputError):
            self.sets.load("base")

    def test_duplicate_json_keys_are_rejected(self):
        path = self.root / "duplicate.json"
        path.write_text('{"value":1,"value":2}', encoding="utf-8")
        with self.assertRaises(UserInputError):
            read_json(path)

    def test_waveform_statistics_match_known_signal(self):
        result = self.csv("time_s,current\n0,-1\n0.1,0\n0.2,1\n")
        signal = result["signals"]["current"]
        self.assertEqual(signal["mean"], 0)
        self.assertEqual(signal["peak_to_peak"], 2)
        self.assertTrue(math.isclose(signal["rms"], math.sqrt(2 / 3)))
        self.assertFalse(result["live_capture"])

    def test_nonuniform_samples_are_reported(self):
        result = self.csv("time_s,current\n0,1\n0.1,2\n0.4,3\n")
        self.assertAlmostEqual(result["sample_step_min_s"], 0.1)
        self.assertAlmostEqual(result["sample_step_max_s"], 0.3)

    def test_waveform_rejects_duplicate_or_reversed_time(self):
        for times in [(0, 0), (1, 0)]:
            with self.subTest(times=times), self.assertRaises(UserInputError):
                self.csv(f"time_s,current\n{times[0]},1\n{times[1]},2\n")

    def test_waveform_rejects_corrupt_or_incomplete_samples(self):
        for text in ["time_s,v\n0,NaN\n", "time_s,v\n0,\n", "time_s,v\n0,1,2\n", "time_s,v,v\n0,1,2\n", "time_s,v\n"]:
            with self.subTest(text=text), self.assertRaises(UserInputError):
                self.csv(text)

    def test_waveform_paths_stay_inside_workspace(self):
        for name in ["../escape.csv", "C:\\escape.csv", "wave.txt"]:
            with self.subTest(name=name), self.assertRaises(UserInputError):
                summarize_waveform(self.root, name)


if __name__ == "__main__":
    unittest.main()
