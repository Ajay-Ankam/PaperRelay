"""Deterministic local exporter for Claude Code session transcripts (PaperRelay only).

Reads native Claude Code JSONL transcripts and writes readable Markdown + plain text,
a chronological tool appendix, a byte copy of each source, SHA-256 hashes and a
conversion manifest. Standard library only. It never edits or deletes a source file.

Usage (PowerShell, repo root):
  .\\.venv\\Scripts\\python.exe scripts\\export_conversations.py --input <session.jsonl> [...] [--out DIR]
  .\\.venv\\Scripts\\python.exe scripts\\export_conversations.py --session-dir <projects dir> --session <uuid>
  ... --hook   (reads Claude Code Stop-hook JSON on stdin; used by .claude/settings.local.json)

What is extracted, verbatim and in recorded (file) order:
  * human prompts: `user` records with promptSource == "typed" (string or text blocks);
  * slash-command invocations (`<command-name>` user records, and `system` records with
    subtype local_command) as user actions;
  * prompts queued while a turn ran (`queued_command` attachments that are not harness
    task notifications), shown once if the same text was also recorded as a typed prompt;
  * user text sent alongside a tool result, labelled as such;
  * "[Request interrupted by user...]" markers, labeled as harness-recorded interruptions;
  * assistant visible text: `text` blocks of `assistant` records, grouped by message.id
    (Claude Code writes one record per content block);
  * tool calls (`tool_use`) and tool results (`tool_result`) -> separate appendix.
What is excluded (counted in the manifest, never silently dropped):
  * `thinking` / `redacted_thinking` blocks (internal reasoning);
  * isMeta / system-injected user records, `<local-command-stdout>`, task notifications
    (task notifications go to the appendix), compaction summaries (model-written, not prompts);
  * request mirrors (`api-request*`), attachments, file-history, mode/permission/title,
    cost and session-owner records (account metadata such as accountUuid is never copied);
  * image blocks (replaced by a placeholder).
Branches (rewind/edit) are not resolved: records are shown in file order.
Hashes show correspondence to the captured file; they are not proof of authenticity.
The Stop-hook transcript can lag the final assistant message (documented by Claude Code),
so hook mode also appends the hook-provided `last_assistant_message` to hook_events.jsonl,
and every run regenerates outputs from the full source, so later runs catch up.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXPORTER_VERSION = "1.0"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "private" / "conversations"
PROJECT_SLUG = re.sub(r"[^A-Za-z0-9]", "-", str(REPO_ROOT))

KNOWN_IGNORED_TYPES = {
    "mode", "permission-mode", "session-owner", "atis-latch", "file-history-snapshot",
    "file-history-delta", "attachment", "last-prompt", "api-request-shape", "api-request-blob",
    "api-request", "ai-title", "tools-used", "queue-operation", "system", "cost-state",
    "pr-link", "summary",
}
REVIEW_BANNER = (
    "REDACTED SUBMISSION COPY -- generated automatically; Ajay must review it before sharing.\n"
    "Redactions cover only secrets and private identifiers (see redaction_log.json); "
    "wording is otherwise unchanged."
)


@dataclass
class Entry:
    kind: str            # prompt | command | assistant | agent_prompt | api_error
    text: str
    ids: list[str]
    timestamp: str
    lines: list[int]
    tool_refs: list[str] = field(default_factory=list)


@dataclass
class ToolEvent:
    kind: str            # call | result | notification
    tool_id: str
    name: str
    body: str
    timestamp: str
    line: int
    is_error: bool = False


@dataclass
class Conversion:
    source: Path
    role: str            # main | subagent
    entries: list[Entry] = field(default_factory=list)
    tools: list[ToolEvent] = field(default_factory=list)
    record_types: Counter[str] = field(default_factory=Counter)
    unknown_types: Counter[str] = field(default_factory=Counter)
    omitted: Counter[str] = field(default_factory=Counter)
    parse_errors: list[int] = field(default_factory=list)
    agent_results: dict[str, str] = field(default_factory=dict)   # agentId -> tool_use_id
    tool_use_ids: set[str] = field(default_factory=set)
    line_count: int = 0


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _block_text(content: Any, conv: Conversion) -> str:
    """Text of a tool_result content (string or list of blocks)."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for b in content or []:
        if not isinstance(b, dict):
            parts.append(str(b))
        elif b.get("type") == "text":
            parts.append(str(b.get("text", "")))
        elif b.get("type") == "image":
            conv.omitted["image_block"] += 1
            parts.append("[image omitted]")
        else:
            conv.omitted[f"tool_result_block:{b.get('type')}"] += 1
            parts.append(f"[{b.get('type')} block omitted]")
    return "\n".join(parts)


def _classify_user_text(rec: dict[str, Any], text: str, role: str) -> str | None:
    s = text.lstrip()
    if rec.get("isCompactSummary"):
        return "omit:compaction_summary"
    if rec.get("isMeta"):
        return "omit:meta_injected"
    if s.startswith("<command-name>"):
        return "command"
    if s.startswith("<local-command-stdout>") or s.startswith("<local-command-stderr>"):
        return "omit:local_command_output"
    if s.startswith("<task-notification>"):
        return "notification"
    if s.startswith("[Request interrupted by user"):
        return "interrupt"
    if rec.get("promptSource") == "typed":
        return "prompt"
    if role == "subagent":
        return "agent_prompt"
    return f"omit:user_untyped_{rec.get('promptSource') or 'none'}"


def convert(path: Path, role: str = "main") -> Conversion:
    conv = Conversion(source=path, role=role)
    current: Entry | None = None
    current_mid: str | None = None
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, start=1):
            conv.line_count = lineno
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                conv.parse_errors.append(lineno)
                continue
            if not isinstance(rec, dict):
                conv.parse_errors.append(lineno)
                continue
            rtype = str(rec.get("type"))
            conv.record_types[rtype] += 1
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else None
            ts = str(rec.get("timestamp", ""))
            uuid = str(rec.get("uuid", ""))
            if rtype == "assistant" and msg is not None:
                mid = str(msg.get("id") or uuid)
                if current is None or current_mid != mid or current.kind not in {"assistant", "api_error"}:
                    kind = "api_error" if rec.get("isApiErrorMessage") else "assistant"
                    current = Entry(kind, "", [mid], ts, [])
                    current_mid = mid
                    conv.entries.append(current)
                if rec.get("isApiErrorMessage"):
                    current.kind = "api_error"
                current.lines.append(lineno)
                if uuid and uuid not in current.ids:
                    current.ids.append(uuid)
                for b in msg.get("content") or []:
                    btype = b.get("type") if isinstance(b, dict) else type(b).__name__
                    if btype == "text":
                        current.text += ("\n\n" if current.text else "") + str(b.get("text", ""))
                    elif btype in {"thinking", "redacted_thinking"}:
                        conv.omitted["assistant_thinking_block"] += 1
                    elif btype == "tool_use":
                        tid = str(b.get("id", ""))
                        conv.tool_use_ids.add(tid)
                        current.tool_refs.append(tid)
                        conv.tools.append(ToolEvent(
                            "call", tid, str(b.get("name", "")),
                            json.dumps(b.get("input"), ensure_ascii=False, indent=2), ts, lineno))
                    else:
                        conv.omitted[f"assistant_block:{btype}"] += 1
                continue
            if rtype == "user" and msg is not None:
                current, current_mid = None, None
                content = msg.get("content")
                tur = rec.get("toolUseResult")
                if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_result":
                            tid = str(b.get("tool_use_id", ""))
                            conv.tools.append(ToolEvent(
                                "result", tid, "", _block_text(b.get("content"), conv), ts,
                                lineno, bool(b.get("is_error"))))
                            if isinstance(tur, dict) and tur.get("agentId"):
                                conv.agent_results[str(tur["agentId"])] = tid
                        elif b.get("type") == "text":
                            # Text riding along with a tool result (e.g. a denial reason):
                            # kept verbatim, labelled, never merged into a typed prompt.
                            conv.entries.append(Entry("with_tool_result", str(b.get("text", "")),
                                                      [uuid], ts, [lineno]))
                        else:
                            conv.omitted[f"user_block_with_tool_result:{b.get('type')}"] += 1
                    continue
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    texts = []
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            texts.append(str(b.get("text", "")))
                        else:
                            conv.omitted[f"user_block:{b.get('type') if isinstance(b, dict) else '?'}"] += 1
                            texts.append("[non-text block omitted]")
                    text = "\n\n".join(texts)
                else:
                    conv.omitted["user_without_content"] += 1
                    continue
                cls = _classify_user_text(rec, text, role)
                if cls is None or cls.startswith("omit:"):
                    conv.omitted[(cls or "omit:unclassified")[5:]] += 1
                elif cls == "notification":
                    conv.tools.append(ToolEvent("notification", "", "task-notification", text, ts, lineno))
                else:
                    conv.entries.append(Entry(cls, text, [uuid], ts, [lineno]))
                continue
            content = rec.get("content")
            if (rtype == "system" and rec.get("subtype") == "local_command"
                    and isinstance(content, str) and content.lstrip().startswith("<command-name>")):
                current, current_mid = None, None
                conv.entries.append(Entry("command", content, [uuid], ts, [lineno]))
                continue
            att = rec.get("attachment")
            if rtype == "attachment" and isinstance(att, dict) and att.get("type") == "queued_command":
                prompt = att.get("prompt")
                if (isinstance(prompt, str) and att.get("commandMode") != "task-notification"
                        and not prompt.lstrip().startswith("<")):
                    current, current_mid = None, None
                    conv.entries.append(Entry("queued_prompt", prompt, [uuid], ts, [lineno]))
                    continue
            if rtype not in KNOWN_IGNORED_TYPES:
                conv.unknown_types[rtype] += 1
            else:
                conv.omitted[f"record:{rtype}"] += 1
    # A queued prompt that the harness also recorded as a typed prompt is shown once.
    typed = {e.text for e in conv.entries if e.kind == "prompt"}
    dupes = [e for e in conv.entries if e.kind == "queued_prompt" and e.text in typed]
    if dupes:
        conv.omitted["queued_prompt_duplicate_of_typed"] += len(dupes)
        conv.entries = [e for e in conv.entries if not any(e is d for d in dupes)]
    return conv


# ---------------------------------------------------------------- rendering

_LABEL = {"prompt": "User prompt", "command": "User slash command", "assistant": "Assistant",
          "agent_prompt": "Delegation prompt (from main agent)", "api_error": "API error message",
          "interrupt": "Interruption marker (recorded by Claude Code)",
          "queued_prompt": "User prompt (queued while a turn was running)",
          "with_tool_result": "User text sent with a tool result"}


def _meta(e: Entry) -> str:
    lines = f"{e.lines[0]}" if len(e.lines) == 1 else f"{e.lines[0]}-{e.lines[-1]}"
    return f"id: {', '.join(i for i in e.ids if i)} | recorded: {e.timestamp} | source lines: {lines}"


def render_markdown(conv: Conversion, title: str) -> str:
    out = [f"# {title}", "", f"Source: `{conv.source.name}` ({conv.role}). Exporter v{EXPORTER_VERSION}. "
           "Verbatim visible text in recorded order; reasoning blocks and harness records are "
           "excluded (see manifest.json). Tool calls/results are in tools_appendix.md.", ""]
    for n, e in enumerate(conv.entries, start=1):
        out.append(f"## {n}. {_LABEL.get(e.kind, e.kind)}")
        out.append(f"<sub>{_meta(e)}</sub>")
        out.append("")
        if e.text:
            out.append(e.text)
        if e.tool_refs:
            note = f"[{len(e.tool_refs)} tool call(s): {', '.join(e.tool_refs)} -> tools_appendix.md]"
            out.append(("\n" if e.text else "") + ("_Tool-only message._ " if not e.text else "") + note)
        if not e.text and not e.tool_refs:
            out.append("_No visible text (only excluded block types)._")
        out.append("")
    return "\n".join(out)


def render_text(conv: Conversion, title: str) -> str:
    out = [title, "=" * len(title), f"Source: {conv.source.name} ({conv.role})", ""]
    for n, e in enumerate(conv.entries, start=1):
        out += [f"--- {n}. {_LABEL.get(e.kind, e.kind).upper()} ---", _meta(e), ""]
        if e.text:
            out.append(e.text)
        if e.tool_refs:
            out.append(f"[{len(e.tool_refs)} tool call(s): {', '.join(e.tool_refs)}; see tools_appendix]")
        out.append("")
    return "\n".join(out)


def render_tools(conv: Conversion, title: str) -> str:
    names = {t.tool_id: t.name for t in conv.tools if t.kind == "call"}
    out = [f"# Tool appendix: {title}", "",
           "Chronological tool calls and results as recorded (verbatim; images omitted).", ""]
    for n, t in enumerate(conv.tools, start=1):
        if t.kind == "call":
            head = f"### T{n}. Call `{t.name}` ({t.tool_id})"
        elif t.kind == "result":
            head = f"### T{n}. Result for `{names.get(t.tool_id, '?')}` ({t.tool_id})" + (
                " [error]" if t.is_error else "")
        else:
            head = f"### T{n}. Harness task notification"
        fence = "~~~~~~" if "```" in t.body or "~~~" in t.body else "```"
        out += [head, f"<sub>recorded: {t.timestamp} | source line: {t.line}</sub>", "",
                fence, t.body, fence, ""]
    return "\n".join(out)


# ---------------------------------------------------------------- redaction

def redaction_rules() -> list[tuple[str, re.Pattern[str], str]]:
    user = Path.home().name
    rules: list[tuple[str, re.Pattern[str], str]] = [
        ("secret", re.compile(r"\b(?:gsk|sk|sk-ant|ghp|gho|github_pat|xox[abp])[_-][A-Za-z0-9_\-]{16,}"),
         "[REDACTED: secret]"),
        ("secret", re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._\-]{16,}"), r"\1 [REDACTED: secret]"),
        # NAME_KEY=value only when the value looks like a credential (>=20 chars, letters and
        # digits, not an attribute/call), so code such as `api_key = settings.x` is untouched.
        ("secret", re.compile(r"(?i)\b([A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD))(\s*[=:]\s*)"
                              r"([\"']?)(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])"
                              r"[A-Za-z0-9_\-]{20,}\3(?![\w.(])"),
         r"\1\2\3[REDACTED: secret]\3"),
        ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[REDACTED: email]"),
    ]
    if user:
        esc = re.escape(user)
        dash = re.escape(re.sub(r"[^A-Za-z0-9]", "-", user))
        rules += [
            ("home_path", re.compile(rf"(?i)([A-Z]:(?:\\\\|\\|/)Users(?:\\\\|\\|/)){esc}"), r"\1[REDACTED: username]"),
            ("home_path", re.compile(rf"(?i)(/[a-z]/Users/){esc}"), r"\1[REDACTED: username]"),
            ("home_path", re.compile(rf"(?i)(C--Users-){dash}"), r"\1[REDACTED: username]"),
            ("account_username", re.compile(rf"(?i){esc}"), "[REDACTED: username]"),
            ("account_username", re.compile(rf"(?i){dash}"), "[REDACTED: username]"),
        ]
        short = re.sub(r"[^A-Za-z0-9]", "", user)[:6].upper()
        if len(short) >= 3:   # Windows 8.3 short form of the home directory, e.g. NAME~1
            rules.append(("account_username", re.compile(rf"(?i)\b{re.escape(short)}~\d\b"), "[REDACTED: username]"))
    return rules


def redact(text: str, counts: Counter[str]) -> str:
    for name, pat, repl in redaction_rules():
        text, n = pat.subn(repl, text)
        counts[name] += n
    return text


# ---------------------------------------------------------------- output

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _copy_raw(src: Path, dst: Path) -> str:
    """Byte copy. An existing copy is replaced only if it is a prefix of the source
    (append-only growth); otherwise the old copy is kept and the new one is saved beside it."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        old = dst.stat().st_size
        with src.open("rb") as a, dst.open("rb") as b:
            if a.read(old) != b.read():
                stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
                alt = dst.with_name(f"{dst.stem}.{stamp}{dst.suffix}")
                shutil.copyfile(src, alt)
                return f"source is not an append-only extension of the earlier copy; saved as {alt.name}"
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    return "copied"


def _summary(conv: Conversion, src_hash: str, out_rel: str, raw_note: str) -> dict[str, Any]:
    kinds = Counter(e.kind for e in conv.entries)
    return {
        "source_name": conv.source.name, "role": conv.role, "source_sha256": src_hash,
        "source_bytes": conv.source.stat().st_size, "source_lines": conv.line_count,
        "output_dir": out_rel, "raw_copy": raw_note,
        "record_types": dict(sorted(conv.record_types.items())),
        "unknown_record_types": dict(conv.unknown_types), "parse_error_lines": conv.parse_errors,
        "extracted": {**dict(kinds), "assistant_tool_only": sum(
            1 for e in conv.entries if e.kind == "assistant" and not e.text and e.tool_refs),
            "tool_calls": sum(1 for t in conv.tools if t.kind == "call"),
            "tool_results": sum(1 for t in conv.tools if t.kind == "result"),
            "task_notifications": sum(1 for t in conv.tools if t.kind == "notification")},
        "omitted": dict(sorted(conv.omitted.items())),
    }


def export_one(src: Path, out_dir: Path, role: str, title: str,
               redacted_dir: Path, redaction_counts: Counter[str]) -> tuple[Conversion, dict[str, Any]]:
    before = sha256(src)
    conv = convert(src, role)
    md, txt, tools = render_markdown(conv, title), render_text(conv, title), render_tools(conv, title)
    _atomic_write(out_dir / "conversation.md", md)
    _atomic_write(out_dir / "conversation.txt", txt)
    _atomic_write(out_dir / "tools_appendix.md", tools)
    raw_note = _copy_raw(src, out_dir / "raw" / src.name)
    raw_note += f"; raw copy sha256 {sha256(out_dir / 'raw' / src.name)}"
    banner_md = "> " + REVIEW_BANNER.replace("\n", "\n> ") + "\n\n"
    _atomic_write(redacted_dir / "conversation.md", banner_md + redact(md, redaction_counts))
    _atomic_write(redacted_dir / "conversation.txt", REVIEW_BANNER + "\n\n" + redact(txt, redaction_counts))
    _atomic_write(redacted_dir / "tools_appendix.md", banner_md + redact(tools, redaction_counts))
    if sha256(src) != before:
        raw_note += "; WARNING: source changed during export (still being written); rerun later"
    return conv, _summary(conv, before, str(out_dir), raw_note)


def export_session(main_src: Path, out_root: Path, include_subagents: bool = True) -> dict[str, Any]:
    session = main_src.stem
    out = out_root / session
    red_root = out / "submission_redacted"
    counts: Counter[str] = Counter()
    main_conv, main_sum = export_one(main_src, out, "main", f"Session {session}", red_root, counts)
    subs: list[dict[str, Any]] = []
    sub_dir = main_src.parent / session / "subagents"
    if include_subagents and sub_dir.is_dir():
        for sp in sorted(sub_dir.glob("agent-*.jsonl")):
            agent_id = sp.stem.removeprefix("agent-")
            meta_path = sp.with_suffix(".meta.json")
            meta: dict[str, Any] = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    meta = {"_error": "meta.json unparseable"}
            evidence = []
            tuid = str(meta.get("toolUseId", ""))
            if tuid and tuid in main_conv.tool_use_ids:
                evidence.append(f"meta.toolUseId {tuid} matches a main-session tool_use id")
            if agent_id in main_conv.agent_results:
                evidence.append(f"main-session tool result for {main_conv.agent_results[agent_id]} "
                                f"carries agentId {agent_id}")
            sub_out = out / "subagents" / sp.stem
            _, s = export_one(sp, sub_out, "subagent",
                              f"Subagent {agent_id} ({meta.get('agentType', 'unknown type')})",
                              red_root / "subagents" / sp.stem, counts)
            s["agent_id"] = agent_id
            s["meta"] = {k: meta.get(k) for k in ("agentType", "description", "toolUseId", "finished")
                         if k in meta}
            s["link"] = {"status": "linked" if evidence else "unlinked (no matching id; not guessed)",
                         "evidence": evidence}
            subs.append(s)
    manifest = {
        "exporter": "scripts/export_conversations.py", "exporter_version": EXPORTER_VERSION,
        "exporter_sha256": sha256(Path(__file__)),
        "generated_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "session_id": session, "main": main_sum, "subagents": subs,
        "redacted_copy": {"dir": "submission_redacted", "status": "UNREVIEWED - Ajay must review before sharing",
                          "redaction_counts": dict(counts)},
        "hash_note": "SHA-256 shows correspondence to the captured file only; it is not "
                     "independent proof of authenticity.",
        "exclusions": "thinking blocks; isMeta/system-injected user records; local command "
                      "output; compaction summaries; api-request mirrors; attachments; account, "
                      "cost, mode, permission and file-history records; images.",
    }
    _atomic_write(out / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    _atomic_write(red_root / "redaction_log.json", json.dumps(
        {"status": "UNREVIEWED", "counts_by_category": dict(counts),
         "categories": [r[0] for r in redaction_rules()]}, indent=2))
    return manifest


# ---------------------------------------------------------------- hook mode

def _projects_dir() -> Path:
    return Path.home() / ".claude" / "projects" / PROJECT_SLUG


def run_hook(stdin_text: str, out_root: Path) -> int:
    """Stop-hook entry. Always returns 0 so Claude's turn is never blocked."""
    try:
        data = json.loads(stdin_text or "{}")
    except json.JSONDecodeError:
        return 0
    tp = Path(str(data.get("transcript_path", "")))
    allowed = _projects_dir().resolve()
    try:
        tp = tp.resolve()
    except OSError:
        return 0
    if tp.parent != allowed:
        return 0   # not a main-session transcript of this project (subagents are found via it)
    if tp.suffix != ".jsonl" or not tp.exists():
        return 0
    session_out = out_root / "auto"
    session_out.mkdir(parents=True, exist_ok=True)
    event = {"received_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
             "hook_event_name": data.get("hook_event_name"), "session_id": data.get("session_id"),
             "stop_hook_active": data.get("stop_hook_active"),
             "last_assistant_message": data.get("last_assistant_message")}
    ev_path = session_out / tp.stem / "hook_events.jsonl"
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    with ev_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    lock = session_out / f".{tp.stem}.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > 300:
            lock.unlink()
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return 0   # another export is running; the next Stop catches up
    try:
        os.close(fd)
        export_session(tp, session_out)
    except Exception as exc:  # noqa: BLE001 - hook must never break the session
        with (session_out / "hook_errors.log").open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now(dt.UTC).isoformat()} {type(exc).__name__}: {exc}\n")
    finally:
        lock.unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", nargs="*", type=Path, default=[], help="main-session transcript .jsonl paths")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT / "exports")
    ap.add_argument("--no-subagents", action="store_true")
    ap.add_argument("--hook", action="store_true", help="read Stop-hook JSON from stdin")
    args = ap.parse_args(argv)
    if args.hook:
        # Bytes -> UTF-8 explicitly: a piped stdin on Windows otherwise decodes with the
        # locale codepage and can raise before run_hook's error handling.
        return run_hook(sys.stdin.buffer.read().decode("utf-8", "replace"), DEFAULT_OUT)
    if not args.input:
        ap.error("--input is required (explicit transcript paths)")
    for p in args.input:
        m = export_session(p, args.out, include_subagents=not args.no_subagents)
        ms = m["main"]
        print(f"{p.name}: lines={ms['source_lines']} entries={ms['extracted']} "
              f"unknown={ms['unknown_record_types']} parse_errors={len(ms['parse_error_lines'])} "
              f"subagents={len(m['subagents'])} -> {args.out / p.stem}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
