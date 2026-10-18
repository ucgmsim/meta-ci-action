You are continuing $context to $repo, unattended. The changes made so far
(by the script, and by any earlier run) are committed or in the working tree;
keep them. The PR can't open until the shared CI's checks pass, and these fail
because of the change:

$gate

1. Read the failures above, then fix each one properly in the code or config.
   `.ai/ci-checks --command <name>` reruns a single check; `.ai/ci-checks`
   runs them all. Rerun until they pass.
2. Don't weaken any check: no lowered thresholds, skipped tests, blanket
   ignores or dropped jobs. A targeted, commented ignore only where code is
   deliberately outside a rule (e.g. a test passing input outside a type hint
   on purpose).
3. Don't change dependency versions or the lockfile to make a check pass.
4. Write or update .ai/pr-title and .ai/pr-body.md: say what you fixed and
   why the change needed it, and list anything you couldn't fix safely with
   the reason.

If a failure can't be fixed safely (it needs a design decision, or the fix
would be large), leave it, say so in .ai/pr-body.md, and finish.
