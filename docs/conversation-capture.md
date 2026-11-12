# Conversation capture workflow

## Automatic export (added 2026-11-12)
- `scripts/export_conversations.py` converts Claude Code's native session transcripts
  for this project into readable files. They live under
  `~/.claude/projects/<project slug>/<session>.jsonl`, with subagents in
  `<session>/subagents/agent-*.jsonl` and `.meta.json`. Run it by hand on explicit paths:
  ```powershell
  .\.venv\Scripts\python.exe scripts\export_conversations.py --input "$HOME\.claude\projects\C--Users-<user>-PaperRelay\<session>.jsonl" --out private\conversations\exports
  ```
- Per session it writes:
  - `conversation.md` and `.txt`: verbatim prompts and visible assistant text, in recorded
    order, with message ids, timestamps and source line numbers;
  - `tools_appendix.md`: tool calls and results, in order;
  - `raw/`: a byte copy of the transcript;
  - `manifest.json`: SHA-256 hashes, record counts, unknown types, parse errors, omitted
    categories and subagent links (with the id evidence for each link, never guessed);
  - `submission_redacted/`: a labelled copy with secrets, emails and the account
    username redacted. It needs Ajay's review before sharing.
- Excluded, and counted in the manifest:
  - thinking blocks;
  - harness-injected records (isMeta, local command output, compaction summaries);
  - request mirrors, attachments, and account, cost and mode records;
  - images.
- **Automatic:** `.claude/settings.local.json` (git-ignored, local only) has a `Stop`
  command hook that runs the exporter with `--hook`.
  - The hook reads `transcript_path` from the hook's stdin, accepts only this project's
    transcript directory, and writes to `private/conversations/auto/<session>/`.
  - It runs `async`, always exits 0, prints nothing and makes no model call, so it
    cannot block, alter or recurse into the session.
  - Claude Code documents that the transcript may lag the final message when Stop fires.
    The hook therefore also appends the hook-supplied `last_assistant_message` to
    `hook_events.jsonl`, and every run regenerates the export from the full transcript,
    so the next Stop catches up.
  - Limitations:
    - the newest turn of a session can be missing from `conversation.md` until the
      next turn ends;
    - user interrupts do not fire Stop;
    - API-error endings fire StopFailure, which is not hooked;
    - errors go to `private/conversations/auto/hook_errors.log`.
- Hashes show that an export corresponds to the captured file. They are not
  independent proof of authenticity.
- The numbered manual files below remain historical records. They are not overwritten
  or completed from memory.

## Manual workflow (historical, sessions before 2026-11-12)

1. Before sending each prompt, save its exact text to a file:
   private/conversations/NN-description-prompt.txt
   (NN = two-digit sequence number; description = short slug)

2. After receiving the assistant's response, copy its full text into:
   private/conversations/NN-description-response.txt

3. Save every follow-up as its own numbered pair in order. Do not combine
   multiple exchanges into one file.

4. Native session records (Claude Code internal logs, if any) remain private
   supporting evidence. Do not read or publish them directly.

5. If a readable transcript export becomes available, preserve the recorded text
   exactly and note any omissions explicitly.

6. Any capture gaps from earlier sessions must be disclosed in the submission.
   Do not reconstruct missing exchanges or invent responses.

7. For submission: compile a chronological index (see below) and include the
   prompt/response files. Redact only API keys, passwords, and unnecessary private
   identifiers; note each redaction location with [REDACTED: reason].

8. These instructions alone do not capture anything automatically. Each exchange
   must be saved manually by Ajay at the time it occurs.

## Chronological index

| # | Slug | Prompt file | Response file | Notes |
|---|------|-------------|---------------|-------|
| 01 | bootstrap | 01-bootstrap-prompt.txt | not saved | Session 1 prompts in one file (renamed from session-01-prompt.txt); first prompt also in docs/FIRST_PROMPT.txt |
| 02 | development-audit | 02-development-audit-prompt.txt | responses/02-development-audit-response.txt | Prompt saved by Ajay before sending; response saved by the assistant at Ajay's request |
| 03 | pipeline | 03-pipeline-prompt.txt | responses/03-pipeline-response.txt | Prompt and response saved by the assistant at Ajay's request |

Add a row for each subsequent exchange in order. Response files live in
private/conversations/responses/.

## Known capture gaps

- Session 1: the prompts were collected into one file (session-01-prompt.txt, now
  01-bootstrap-prompt.txt) after the session rather than saved one by one beforehand.
  No Session 1 response files exist.
- Response files written by the assistant contain its user-visible text only. Tool
  calls and tool output are left out, and each file says so in its header.
- Native session records may exist but have not been read or verified.
