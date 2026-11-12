"""Assemble the submission folder (does not upload or submit anything).

    python scripts/assemble_submission.py --out submission/

Copies: report.pdf (if present), source.zip (tracked files at HEAD via `git ls-files`,
read-only; excluding .env*, .venv, the private brief, caches), a canonical results.csv
concatenated from real + stand-in runs with results_manifest.json, evidence/ (real and
stand-in only), standin/ calibration + configs, experiments/, eval/ questions + grades,
reports/, and readable conversation files (private/conversations/*.txt and
private/conversations/responses/*.txt). Every copied text file, and every text member of
source.zip, is scanned for secret patterns; any hit fails the assembly and the output is
not trusted. Writes CHECKLIST.md marking each deliverable present/missing; human items
(report, grades, Vaani feedback, reflection) are never fabricated.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from paperrelay.client.smoke import COLUMNS  # noqa: E402

# Value-shaped patterns: they match real credentials, not prose that merely mentions
# "Authorization" (the source code and docs legitimately name the header).
SECRET_PATTERNS = [
    re.compile(r"gsk_[A-Za-z0-9]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"Authorization[\"']?\s*[:=]\s*[\"']?(?:Bearer\s+)?[A-Za-z0-9._\-]{16,}",
               re.IGNORECASE),
    # quoted literal (code/JSON) or env-file style without spaces; not `api_key = func()`
    re.compile(r"api_key[\"']?\s*[:=]\s*[\"'][A-Za-z0-9._\-]{16,}[\"']", re.IGNORECASE),
    re.compile(r"api_key=[A-Za-z0-9._\-]{16,}(?![A-Za-z0-9._\-]*\()", re.IGNORECASE),
    re.compile(r"GROQ_API_KEY\s*=\s*[\"']?[A-Za-z0-9._\-]{8,}"),
]
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".jsonl", ".py", ".toml", ".cfg", ".ini",
                 ".yaml", ".yml", ".ps1", ".sh", ".html", ".lock", ".gitignore", ""}
EXCLUDE_PARTS = {".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
                 ".ruff_cache", ".git", "node_modules", ".claude", "submission"}
EXCLUDE_NAMES = {"assignment_description.md", "assignment-description.md",
                 "call_ledger.lock", "call_ledger.tmp"}
TABLE_SOURCES = ("real", "stand-in")


class AssemblyError(RuntimeError):
    pass


def excluded(rel: Path) -> bool:
    if any(part in EXCLUDE_PARTS for part in rel.parts):
        return True
    if rel.name in EXCLUDE_NAMES or rel.name.startswith(".env"):
        return True
    return rel.suffix in (".pyc", ".pyo") or rel.name.endswith(".egg-info")


def scan_text(text: str) -> list[str]:
    return [p.pattern for p in SECRET_PATTERNS if p.search(text)]


def scan_file(path: Path) -> list[str]:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
    try:
        return scan_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def tracked_files(repo: Path) -> list[Path]:
    """Files tracked at HEAD (read-only git query)."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True, check=True)
    return [Path(p) for p in out.stdout.decode("utf-8").split("\0") if p]


def build_source_zip(repo: Path, files: list[Path], out: Path) -> tuple[int, list[str]]:
    hits: list[str] = []
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(files):
            src = repo / rel
            if excluded(rel) or not src.is_file():
                continue
            for pattern in scan_file(src):
                hits.append(f"source.zip:{rel.as_posix()}: {pattern}")
            zf.write(src, rel.as_posix())
            n += 1
    return n, hits


def copy_tree(src: Path, dst: Path, hits: list[str], root: Path) -> int:
    n = 0
    if not src.exists():
        return 0
    paths = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
    for p in paths:
        rel = p.relative_to(src.parent if src.is_file() else src)
        if excluded(rel) or excluded(Path(p.name)):
            continue
        target = dst / rel if src.is_dir() else dst
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        for pattern in scan_file(target):
            hits.append(f"{target.relative_to(root).as_posix()}: {pattern}")
        n += 1
    return n


def canonical_results(evidence: Path, out: Path) -> dict[str, object]:
    """Concatenate real + stand-in results.csv (never offline-double) in COLUMNS order."""
    runs = []
    rows_total = 0
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for source in TABLE_SOURCES:
            for path in sorted((evidence / source).glob("*/results.csv")):
                with open(path, newline="", encoding="utf-8") as rf:
                    reader = csv.DictReader(rf)
                    if reader.fieldnames != COLUMNS:
                        raise AssemblyError(f"{path}: columns differ from the contract")
                    rows = list(reader)
                bad = {r["source"] for r in rows} - {source}
                if bad:
                    raise AssemblyError(f"{path}: rows with source {bad} under {source}/")
                writer.writerows(rows)
                rows_total += len(rows)
                runs.append({"source": source, "run_id": path.parent.name, "rows": len(rows),
                             "path": path.relative_to(evidence.parent).as_posix(),
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"generated_utc": datetime.now(UTC).isoformat(), "rows": rows_total, "runs": runs,
            "excluded": "offline-double and client-only runs are never included"}


def assemble(repo: Path, out: Path, files: list[Path] | None = None) -> dict[str, object]:
    if out.exists() and any(out.iterdir()):
        raise AssemblyError(f"{out} is not empty; choose a new folder")
    out.mkdir(parents=True, exist_ok=True)
    hits: list[str] = []
    status: dict[str, object] = {}

    report = next((p for p in (repo / "report.pdf", repo / "reports" / "report.pdf")
                   if p.exists()), None)
    if report:
        shutil.copy2(report, out / "report.pdf")
    status["report.pdf"] = bool(report)

    n_src, src_hits = build_source_zip(repo, files if files is not None else tracked_files(repo),
                                       out / "source.zip")
    hits += src_hits
    status["source.zip"] = n_src > 0

    manifest = canonical_results(repo / "evidence", out / "results.csv")
    (out / "results_manifest.json").write_text(json.dumps(manifest, indent=2),
                                               encoding="utf-8")
    for p in scan_file(out / "results.csv"):
        hits.append(f"results.csv: {p}")
    status["results.csv"] = int(manifest["rows"]) > 0  # type: ignore[call-overload]

    counts = {}
    for source in TABLE_SOURCES:
        counts[f"evidence/{source}"] = copy_tree(repo / "evidence" / source,
                                                 out / "evidence" / source, hits, out)
    counts["standin"] = copy_tree(repo / "standin", out / "standin", hits, out)
    counts["experiments"] = copy_tree(repo / "experiments", out / "experiments", hits, out)
    counts["eval"] = copy_tree(repo / "eval", out / "eval", hits, out)
    counts["reports"] = copy_tree(repo / "reports", out / "reports", hits, out)
    conv = repo / "private" / "conversations"
    n_conv = 0
    for sub in (conv, conv / "responses"):
        for p in sorted(sub.glob("*.txt")) if sub.exists() else []:
            n_conv += copy_tree(p, out / "conversations" / p.relative_to(conv), hits, out)
    counts["conversations"] = n_conv
    for doc in ("README.md",):
        if (repo / doc).exists():
            copy_tree(repo / doc, out / doc, hits, out)

    calib = list((repo / "standin").glob("calibration-*.json")) if (repo / "standin").exists() \
        else []
    grade_files = sorted((repo / "eval").glob("grades_*.csv")) if (repo / "eval").exists() else []
    graded = 0
    for g in grade_files:
        with open(g, newline="", encoding="utf-8") as fh:
            graded += sum(1 for r in csv.DictReader(fh) if (r.get("answer_grade") or "").strip())
    items = [
        ("report.pdf (written by Ajay)", status["report.pdf"]),
        ("source.zip", status["source.zip"]),
        ("results.csv (real + stand-in, manifest included)", status["results.csv"]),
        ("raw evidence: evidence/real", counts["evidence/real"] > 0),
        ("raw evidence: evidence/stand-in", counts["evidence/stand-in"] > 0),
        ("stand-in calibration (standin/calibration-*.json)", bool(calib)),
        ("experiment configs (experiments/)", counts["experiments"] > 0),
        ("evaluation questions (eval/questions.csv, Ajay-authored)",
         (repo / "eval" / "questions.csv").exists()),
        (f"manual grades (eval/grades_*.csv; {graded} graded rows; human item)", graded > 0),
        ("results tables (reports/tables.md)", (repo / "reports" / "tables.md").exists()),
        ("conversation history (private/conversations/*.txt)", n_conv > 0),
        ("Vaani website feedback (human item, inside report)", None),
        ("reflection (human item, inside report)", None),
        ("README commands", (repo / "README.md").exists()),
    ]
    lines = ["# Submission checklist", "",
             f"Generated {datetime.now(UTC).isoformat()} by scripts/assemble_submission.py. "
             "Assembly does not upload or submit; Ajay controls sharing and the form.", ""]
    for name, ok in items:
        mark = "present" if ok else ("HUMAN ITEM: not verifiable by script; check the report"
                                     if ok is None else "MISSING")
        lines.append(f"- [{'x' if ok else ' '}] {name}: {mark}")
    lines += ["", f"Secret scan: {'FAILED' if hits else 'passed'} "
                  f"({len(hits)} finding(s)).", ""]
    (out / "CHECKLIST.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = {"status": status, "counts": counts, "secret_hits": hits}
    if hits:
        raise AssemblyError("secret patterns found:\n" + "\n".join(hits))
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Assemble the submission folder")
    parser.add_argument("--out", default="submission")
    args = parser.parse_args(argv)
    try:
        result = assemble(REPO, Path(args.out).resolve())
    except (AssemblyError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"assembly failed: {exc}") from exc
    print(json.dumps(result["counts"]))


if __name__ == "__main__":
    main()
