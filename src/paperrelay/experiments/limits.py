"""Provider-limit evidence (Table 1): schema, header prefill and rendering.

experiments/limits_evidence.json keeps, for each limit item, three separately sourced
entries that are never merged:
- documented: from provider documentation (main session fills, with URL + checked date);
- header_advertised: parsed from x-ratelimit-* headers in real results.csv
  limit_snapshot_json (this module fills it, deterministically, from real evidence);
- observed: empirically measured behaviour (e.g. a 429 and its recovery from the limit
  probe). Default "not measured".
Each entry: {value, evidence, confidence}. Header interpretation follows the Groq rate
limit docs as recorded in docs/interfaces-m3.md: x-ratelimit-*-requests refers to
requests per DAY (RPD) and x-ratelimit-*-tokens to tokens per MINUTE (TPM); re-verify the
current docs before relying on it.

    python -m paperrelay.experiments.limits --prefill evidence/real/<run>/results.csv ...
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from paperrelay.config import REPO_ROOT

ITEMS = ("rpm", "tpm", "rpd", "tpd", "concurrency", "limit_response", "recovery")
KINDS = ("documented", "header_advertised", "observed")
LABELS = {"rpm": "Requests per minute (RPM)", "tpm": "Tokens per minute (TPM)",
          "rpd": "Requests per day (RPD)", "tpd": "Tokens per day (TPD)",
          "concurrency": "Concurrent requests", "limit_response": "Limit response",
          "recovery": "Recovery"}
DEFAULT_PATH = REPO_ROOT / "experiments" / "limits_evidence.json"
# header name -> item it advertises (per Groq docs; see module docstring)
HEADER_ITEMS = {"x-ratelimit-limit-requests": "rpd", "x-ratelimit-limit-tokens": "tpm"}


def not_measured(kind: str) -> dict[str, Any]:
    return {"value": None, "evidence": "not measured", "confidence": "none",
            "kind": kind}


def empty_evidence(model: str = "openai/gpt-oss-20b") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model": model,
        "notes": "value/evidence/confidence per item and kind; kinds are never merged. "
                 "Unknown concurrency is not an established ceiling.",
        "items": {item: {kind: not_measured(kind) for kind in KINDS} for item in ITEMS},
    }


def load(path: Path = DEFAULT_PATH) -> dict[str, Any]:
    if not path.exists():
        return empty_evidence()
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    validate(data)
    return data


def validate(data: dict[str, Any]) -> None:
    items = data.get("items")
    if not isinstance(items, dict) or set(items) != set(ITEMS):
        raise ValueError(f"limits evidence must contain exactly items {ITEMS}")
    for item, kinds in items.items():
        if set(kinds) != set(KINDS):
            raise ValueError(f"{item}: kinds must be exactly {KINDS}")
        for kind, entry in kinds.items():
            for key in ("value", "evidence", "confidence"):
                if key not in entry:
                    raise ValueError(f"{item}.{kind} missing {key}")
            if entry["value"] is not None and entry["evidence"] in ("", "not measured"):
                raise ValueError(f"{item}.{kind} has a value without evidence")


def header_advertised(csv_paths: list[Path]) -> dict[str, dict[str, Any]]:
    """Distinct advertised limit values per item from real results.csv snapshots."""
    seen: dict[str, Counter[str]] = {}
    sources: dict[str, set[str]] = {}
    for path in csv_paths:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("source") != "real" or not row.get("limit_snapshot_json"):
                    continue
                try:
                    snap = json.loads(row["limit_snapshot_json"])
                except ValueError:
                    continue
                for header, item in HEADER_ITEMS.items():
                    if header in snap:
                        seen.setdefault(item, Counter())[str(snap[header])] += 1
                        sources.setdefault(item, set()).add(row.get("run_id") or path.parent.name)
    out: dict[str, dict[str, Any]] = {}
    for item, counts in seen.items():
        header = next(h for h, i in HEADER_ITEMS.items() if i == item)
        values = sorted(counts)
        n = sum(counts.values())
        runs = ", ".join(sorted(sources[item]))
        consistent = len(values) == 1
        out[item] = {
            "value": int(values[0]) if consistent and values[0].isdigit() else values,
            "evidence": f"{header} in limit_snapshot_json of {n} real results.csv row(s) "
                        f"(runs: {runs}); interpreted as {item.upper()} per Groq docs "
                        f"(re-verify current docs)",
            "confidence": ("medium: consistent across rows; header meaning taken from docs"
                           if consistent else "low: values differ across rows"),
            "kind": "header_advertised",
            "rows": n,
        }
    return out


def prefill(data: dict[str, Any], csv_paths: list[Path]) -> dict[str, Any]:
    found = header_advertised(csv_paths)
    for item in ITEMS:
        if item in found:
            data["items"][item]["header_advertised"] = found[item]
        elif item in ("rpm", "tpd"):
            data["items"][item]["header_advertised"] = {
                "value": None, "kind": "header_advertised", "confidence": "none",
                "evidence": "not advertised in recorded x-ratelimit-* headers"}
    return data


def table_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in ITEMS:
        for kind in KINDS:
            e = data["items"][item][kind]
            value = e["value"]
            rows.append({"item": item, "label": LABELS[item], "kind": kind,
                         "value": "not measured" if value is None else value,
                         "evidence": e["evidence"], "confidence": e["confidence"]})
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Prefill header-advertised limit evidence")
    parser.add_argument("--prefill", nargs="+", required=True, help="real results.csv files")
    parser.add_argument("--path", default=str(DEFAULT_PATH))
    args = parser.parse_args(argv)
    path = Path(args.path)
    data = load(path) if path.exists() else empty_evidence()
    data = prefill(data, [Path(p) for p in args.prefill])
    validate(data)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"updated header_advertised entries in {path}")


if __name__ == "__main__":
    main()
