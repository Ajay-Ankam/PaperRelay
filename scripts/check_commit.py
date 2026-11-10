"""Repository commit policy; all subjects use the same word limit."""
import re
import sys
from pathlib import Path

def validate(message: str) -> list[str]:
    lines = message.strip().splitlines()
    if not lines:
        return ["Empty commit message"]
    errors = []
    if re.search(r"(?im)^\s*co-authored-by\s*:", message):
        errors.append("Co-authored-by trailers are prohibited by repository policy")
    if re.search(r"(?i)generated (?:with|by) (?:claude|anthropic)|noreply@anthropic", message):
        errors.append("AI attribution is prohibited in commit messages")
    count = len(lines[0].split())
    if not 8 <= count <= 10:
        errors.append(f"Commit subject needs 8-10 words; found {count}")
    return errors

if __name__ == "__main__":
    errors = validate(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)
