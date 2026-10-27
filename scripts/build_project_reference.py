"""Extract reference definitions via the user's plaintext-capable rg executable."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def build(path: Path, rg: str) -> dict:
    completed = subprocess.run([rg, "--text", "--no-line-number", "--no-filename", "--color", "never", "^", str(path)],
                               stdin=subprocess.DEVNULL, capture_output=True, check=True)
    document = ET.fromstring(completed.stdout)
    groups = document.findall(".//Group/G")
    variables = document.findall(".//Variable/Vars")
    rows = []
    index = 0
    for group in groups:
        code = group.attrib["groupname"].split("组")[0]
        for offset in range(int(group.attrib["grouplen"])):
            value = variables[index].attrib
            attribute = int(value["chaname"])
            rows.append({"code": f"{code}.{offset:02d}", "group": int(group.attrib["groupfuction"]),
                         "offset": offset, "group_name": group.attrib["groupname"], "name": value["varname"],
                         "attribute_raw": attribute, "signed": bool((attribute >> 18) & 1),
                         "width_bits": 32 if (attribute >> 29) & 1 else 16,
                         "decimal_places_reference": int(value["currentdot"]),
                         "unit_index": (attribute >> 4) & 63, "access": (attribute >> 12) & 3,
                         "enum_reference": value.get("comboname", "")})
            index += 1
    if index != len(variables):
        raise ValueError("Group counts do not match the exported definitions.")
    return {"source": str(path.resolve()), "plaintext_sha256": hashlib.sha256(completed.stdout).hexdigest(),
            "platform": 9, "parameter_version_reference": "0.11", "reference_only": True,
            "values_are_live": False, "parameters": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--rg", default="rg")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reference = build(args.source, args.rg)
    args.output.write_text(json.dumps(reference, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"parameters": len(reference["parameters"]), "output": str(args.output)}))
