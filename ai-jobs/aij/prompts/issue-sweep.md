You are reviewing part of $repo for bugs and worthwhile improvements,
unattended. Nobody will answer questions. You do not change code. Your only
output is .ai/issues.json, which a script turns into GitHub issues.

Focus on these files this week (read others as needed for context):
$focus

These issues already exist. Do not file duplicates or near-duplicates:
$existing

1. Read CLAUDE.md if it exists, then the focus files and their tests.
2. Look for, in priority order:
   - correctness bugs: wrong results, crashes on valid input, unit or sign
     errors, off-by-one, wrong axis, silent NaN or overflow, broken edge cases
     (zero-length, dip of 0 or 90, empty arrays), resources not closed;
   - missing tests for important behaviour;
   - small, clear improvements: dead code, duplicated logic, misleading names
     or docstrings, deprecated API usage.
3. Confirm every bug before reporting it. Write a minimal reproducer and run
   it with `uv run --no-sync python -c ...` or a scratch file under .ai/.
   Report only what you confirmed, or can show by reading the code with no
   reasonable doubt.
4. Report at most $max_issues issues. Fewer, well-evidenced issues are better
   than many speculative ones. Report nothing if you found nothing solid.

Write .ai/issues.json as a JSON list. Each entry:
{
  "title": "short, specific, imperative or descriptive",
  "kind": "bug" | "tests" | "refactor" | "docs",
  "severity": "critical" | "high" | "medium" | "low",
  "ai_ok": true | false,
  "files": ["path/relative/to/repo.py"],
  "body": "markdown: location (file:line), what is wrong, reproducer and its output, suggested fix"
}

Severity: `critical` or `high` is a bug that silently produces wrong
scientific results or crashes normal use. Everything else is `medium` or `low`.

Set `ai_ok` to true only when the fix is small (under ~150 lines), the correct
behaviour is unambiguous, and it doesn't change physics or numerical results
beyond fixing the bug. When in doubt, false.
