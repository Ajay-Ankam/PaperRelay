# Conversation capture workflow

This document describes how to preserve the assistant conversation history required
for submission. It does not capture conversations automatically.

## Workflow

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
