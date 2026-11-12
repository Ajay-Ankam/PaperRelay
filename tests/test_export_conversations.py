"""Exporter tests on synthetic records shaped like Claude Code transcripts."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "export_conversations", Path(__file__).parents[1] / "scripts" / "export_conversations.py")
assert _spec and _spec.loader
ex = importlib.util.module_from_spec(_spec)
sys.modules["export_conversations"] = ex
_spec.loader.exec_module(ex)

SECRET = "gsk_" + "A1b2C3d4E5f6G7h8I9j0K1l2"   # synthetic, split so no literal key sits in source


def _rec(**kw: Any) -> str:
    return json.dumps(kw)


def _session(tmp: Path, sid: str = "sess-1") -> Path:
    lines = [
        _rec(type="mode", mode="default"),
        _rec(type="session-owner", accountUuid="acct-PRIVATE", organizationUuid="org-PRIVATE"),
        _rec(type="user", uuid="u1", timestamp="t1", promptSource="typed",
             message={"role": "user", "content": "Run the steady scenario.\nexact  spacing kept"}),
        _rec(type="api-request-blob", message={"role": "user", "content": [
            {"type": "text", "text": "DUPLICATE MIRROR"}]}),
        _rec(type="assistant", uuid="a1", timestamp="t2", message={
            "id": "msg_1", "role": "assistant", "content": [
                {"type": "thinking", "thinking": "PRIVATE REASONING"}]}),
        _rec(type="assistant", uuid="a2", timestamp="t2", message={
            "id": "msg_1", "role": "assistant", "content": [{"type": "text", "text": "Checking ports."}]}),
        _rec(type="assistant", uuid="a3", timestamp="t2", message={
            "id": "msg_1", "role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "ls"}}]}),
        _rec(type="user", uuid="u2", timestamp="t3", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": [
                {"type": "text", "text": "file_a\nfile_b"}, {"type": "image", "source": {}}]}]}),
        _rec(type="assistant", uuid="a4", timestamp="t4", message={
            "id": "msg_2", "role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_2", "name": "Agent", "input": {"prompt": "review"}}]}),
        _rec(type="user", uuid="u3", timestamp="t5", toolUseResult={"agentId": "abc"},
             message={"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "tu_2", "content": "review done"}]}),
        "{not json",
        _rec(type="user", uuid="u4", isMeta=True, message={"role": "user", "content": "<system-reminder>x"}),
        _rec(type="user", uuid="u5", isCompactSummary=True, message={"role": "user", "content": "summary"}),
        _rec(type="user", uuid="u6", promptSource="system",
             message={"role": "user", "content": "<task-notification>done</task-notification>"}),
        _rec(type="user", uuid="u7", message={"role": "user",
                                              "content": "<command-name>/context</command-name>"}),
        _rec(type="user", uuid="u9", interruptedMessageId="msg_1", message={"role": "user", "content": [
            {"type": "text", "text": "[Request interrupted by user for tool use]"}]}),
        _rec(type="brand-new-type", foo=1),
        _rec(type="user", uuid="u8", timestamp="t6", promptSource="typed", message={
            "role": "user", "content": [{"type": "text", "text": f"key {SECRET} mail a.b@example.com "
                                                                 f"path C:\\Users\\{Path.home().name}\\x"}]}),
        _rec(type="assistant", uuid="a5", timestamp="t7", message={
            "id": "msg_3", "role": "assistant", "content": [{"type": "text", "text": "Done."}]}),
    ]
    proj = tmp / "projects"
    proj.mkdir(parents=True, exist_ok=True)
    main = proj / f"{sid}.jsonl"
    main.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sub = proj / sid / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-abc.jsonl").write_text("\n".join([
        _rec(type="user", uuid="s1", isSidechain=True, message={"role": "user", "content": "Review X"}),
        _rec(type="assistant", uuid="s2", isSidechain=True, message={
            "id": "m_s", "role": "assistant", "content": [{"type": "text", "text": "Found 2 issues."}]}),
    ]) + "\n", encoding="utf-8")
    (sub / "agent-abc.meta.json").write_text(json.dumps(
        {"agentType": "quality-reviewer", "toolUseId": "tu_2"}), encoding="utf-8")
    (sub / "agent-zzz.jsonl").write_text(_rec(
        type="user", uuid="z1", message={"role": "user", "content": "orphan"}) + "\n", encoding="utf-8")
    return main


def test_extracts_verbatim_in_order_and_groups_blocks(tmp_path: Path) -> None:
    conv = ex.convert(_session(tmp_path))
    kinds = [e.kind for e in conv.entries]
    assert kinds == ["prompt", "assistant", "assistant", "command", "interrupt", "prompt", "assistant"]
    assert conv.entries[0].text == "Run the steady scenario.\nexact  spacing kept"
    first = conv.entries[1]
    assert first.text == "Checking ports." and first.tool_refs == ["tu_1"]
    assert first.ids[0] == "msg_1" and first.lines == [5, 6, 7]
    tool_only = conv.entries[2]
    assert tool_only.text == "" and tool_only.tool_refs == ["tu_2"]


def test_exclusions_and_errors_are_counted_not_silent(tmp_path: Path) -> None:
    conv = ex.convert(_session(tmp_path))
    assert conv.parse_errors == [11]
    assert dict(conv.unknown_types) == {"brand-new-type": 1}
    for k in ("assistant_thinking_block", "meta_injected", "compaction_summary",
              "record:api-request-blob", "image_block"):
        assert conv.omitted[k] >= 1, k
    assert [t.kind for t in conv.tools] == ["call", "result", "call", "result", "notification"]
    assert conv.tools[1].body == "file_a\nfile_b\n[image omitted]"


def test_export_writes_outputs_hashes_links_and_never_touches_source(tmp_path: Path) -> None:
    main = _session(tmp_path)
    before = hashlib.sha256(main.read_bytes()).hexdigest()
    m = ex.export_session(main, tmp_path / "out")
    assert hashlib.sha256(main.read_bytes()).hexdigest() == before == m["main"]["source_sha256"]
    out = tmp_path / "out" / "sess-1"
    md = (out / "conversation.md").read_text(encoding="utf-8")
    assert "PRIVATE REASONING" not in md and "DUPLICATE MIRROR" not in md
    assert "acct-PRIVATE" not in json.dumps(m) and "acct-PRIVATE" not in md
    assert "Checking ports." in md and (out / "conversation.txt").exists()
    assert '"command": "ls"' in (out / "tools_appendix.md").read_text(encoding="utf-8")
    assert (out / "raw" / "sess-1.jsonl").read_bytes() == main.read_bytes()
    links = {s["agent_id"]: s["link"] for s in m["subagents"]}
    assert links["abc"]["status"] == "linked" and len(links["abc"]["evidence"]) == 2
    assert links["zzz"]["status"].startswith("unlinked")
    sub_md = (out / "subagents" / "agent-abc" / "conversation.md").read_text(encoding="utf-8")
    assert "Delegation prompt" in sub_md and "Found 2 issues." in sub_md


def test_redacted_copy_is_labeled_and_private_original_unchanged(tmp_path: Path) -> None:
    ex.export_session(_session(tmp_path), tmp_path / "out")
    out = tmp_path / "out" / "sess-1"
    original = (out / "conversation.md").read_text(encoding="utf-8")
    red = (out / "submission_redacted" / "conversation.md").read_text(encoding="utf-8")
    assert SECRET in original and "a.b@example.com" in original
    assert SECRET not in red and "a.b@example.com" not in red
    assert Path.home().name not in red and "[REDACTED: username]" in red
    assert red.startswith("> REDACTED SUBMISSION COPY") and "must review" in red
    log = json.loads((out / "submission_redacted" / "redaction_log.json").read_text(encoding="utf-8"))
    assert log["status"] == "UNREVIEWED" and log["counts_by_category"]["secret"] >= 1


def test_redaction_rules_hit_credentials_but_not_code_identifiers() -> None:
    from collections import Counter
    c: Counter[str] = Counter()
    assert ex.redact('ANSWER_TOKEN = "answer_token"', c) == 'ANSWER_TOKEN = "answer_token"'
    assert ex.redact("api_key = settings.groq_api_key", c) == "api_key = settings.groq_api_key"
    assert ex.redact("GROQ_API_KEY=abcd1234efgh5678ijkl9012", c) == "GROQ_API_KEY=[REDACTED: secret]"
    assert ex.redact("Authorization: Bearer abcdefghijklmnop1234", c).endswith("Bearer [REDACTED: secret]")
    assert c["secret"] == 2


def _write(tmp: Path, lines: list[str]) -> Path:
    p = tmp / "s.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_commands_queued_prompts_mixed_text_and_api_errors_are_kept(tmp_path: Path) -> None:
    p = _write(tmp_path, [
        _rec(type="system", subtype="local_command", uuid="c1",
             content="<command-name>/context</command-name>"),
        _rec(type="system", subtype="local_command", uuid="c2",
             content="<local-command-stdout>big output</local-command-stdout>"),
        _rec(type="attachment", uuid="q1", attachment={
            "type": "queued_command", "prompt": "also check burst", "commandMode": "prompt"}),
        _rec(type="attachment", uuid="q2", attachment={
            "type": "queued_command", "prompt": "<task-notification>x</task-notification>",
            "commandMode": "task-notification"}),
        _rec(type="attachment", uuid="q3", attachment={
            "type": "queued_command", "prompt": "same text twice", "commandMode": "prompt"}),
        _rec(type="user", uuid="u1", promptSource="typed",
             message={"role": "user", "content": "same text twice"}),
        _rec(type="user", uuid="u2", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "denied", "is_error": True},
            {"type": "text", "text": "use the other port"}]}),
        _rec(type="assistant", uuid="a1", message={"id": "m1", "content": [
            {"type": "text", "text": "partial"}]}),
        _rec(type="assistant", uuid="a2", isApiErrorMessage=True, message={"id": "m1", "content": [
            {"type": "text", "text": "API Error: overloaded"}]}),
    ])
    conv = ex.convert(p)
    got = [(e.kind, e.text) for e in conv.entries]
    assert got == [("command", "<command-name>/context</command-name>"),
                   ("queued_prompt", "also check burst"),
                   ("prompt", "same text twice"),
                   ("with_tool_result", "use the other port"),
                   ("api_error", "partial\n\nAPI Error: overloaded")]
    assert conv.omitted["queued_prompt_duplicate_of_typed"] == 1
    assert "big output" not in "".join(e.text for e in conv.entries)


def test_hook_rejects_subagent_files_and_decodes_utf8_stdin(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    main = _session(tmp_path)
    monkeypatch.setattr(ex, "_projects_dir", lambda: main.parent)
    sub = main.parent / "sess-1" / "subagents" / "agent-abc.jsonl"
    assert ex.run_hook(json.dumps({"transcript_path": str(sub)}), tmp_path / "o") == 0
    assert not (tmp_path / "o").exists()
    monkeypatch.setattr(ex, "DEFAULT_OUT", tmp_path / "o2")
    payload = json.dumps({"transcript_path": str(main), "session_id": "sess-1",
                          "last_assistant_message": "naïve – résumé ✓"}, ensure_ascii=False)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")),
                                                       encoding="cp1252"))
    assert ex.main(["--hook"]) == 0
    ev = (tmp_path / "o2" / "auto" / "sess-1" / "hook_events.jsonl").read_text(encoding="utf-8")
    assert json.loads(ev)["last_assistant_message"] == "naïve – résumé ✓"


def test_raw_copy_keeps_old_copy_when_source_is_not_append_only(tmp_path: Path) -> None:
    main = _session(tmp_path)
    ex.export_session(main, tmp_path / "out")
    main.write_text(main.read_text(encoding="utf-8") + _rec(type="mode") + "\n", encoding="utf-8")
    ex.export_session(main, tmp_path / "out")          # append-only growth: replaced in place
    raw_dir = tmp_path / "out" / "sess-1" / "raw"
    assert sorted(p.name for p in raw_dir.iterdir()) == ["sess-1.jsonl"]
    main.write_text("rewritten\n", encoding="utf-8")
    m = ex.export_session(main, tmp_path / "out")
    assert "not an append-only" in m["main"]["raw_copy"]
    assert len(list(raw_dir.iterdir())) == 2


def test_hook_mode_only_accepts_this_projects_transcripts(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    main = _session(tmp_path)
    monkeypatch.setattr(ex, "_projects_dir", lambda: main.parent)
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text(main.read_text(encoding="utf-8"), encoding="utf-8")
    assert ex.run_hook(json.dumps({"transcript_path": str(outside)}), tmp_path / "o1") == 0
    assert not (tmp_path / "o1").exists()
    assert ex.run_hook("not json", tmp_path / "o1") == 0
    payload = {"transcript_path": str(main), "hook_event_name": "Stop", "session_id": "sess-1",
               "stop_hook_active": False, "last_assistant_message": "Done."}
    assert ex.run_hook(json.dumps(payload), tmp_path / "o2") == 0
    auto = tmp_path / "o2" / "auto" / "sess-1"
    assert (auto / "conversation.md").exists() and (auto / "manifest.json").exists()
    ev = [json.loads(x) for x in (auto / "hook_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert ev[0]["last_assistant_message"] == "Done."
    assert not list((tmp_path / "o2" / "auto").glob(".*.lock"))
