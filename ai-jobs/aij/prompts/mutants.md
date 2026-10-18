You are strengthening tests in $repo, unattended. Nobody will answer
questions. Your output is new or extended tests plus two files under .ai/.
A script checks your work, re-runs mutation testing and opens the PR.

Mutation testing changed `$target` in small ways ("mutants"), and the test
suite still passed for each of these. That means no test checks the
behaviour the mutant broke:

$survivors

1. Read CLAUDE.md if it exists, then the target file and its existing tests.
2. For each mutant, decide whether the change matters. Some are equivalent to
   the original (for example, changing a bound that can never be reached, or a
   log message). Skip those.
3. For those that matter, write tests that would fail under the mutant and
   pass on the real code: assertions on exact values, boundary cases, error
   conditions. Follow the existing test style. For Rust, put tests where the
   crate already keeps them. For Python, put them under tests/.
4. Only add or change test code. Never edit the code under test.
5. Run `.ai/ci-checks`, which runs every check CI runs. Anything that passed
   before your change must still pass. CI type-checks tests too: if a test
   deliberately passes input outside a type hint, add a targeted
   `# ty: ignore[<rule>]` on that line saying why.
6. Write .ai/pr-title (one line, imperative, under 70 characters) and
   .ai/pr-body.md listing which mutants the new tests kill, and which you
   judged equivalent and why.

If a mutant exposes a real bug (the mutated code is *more* correct than the
original, or the original is wrong on a boundary), don't write a test that
locks in the wrong behaviour. Describe it in .ai/bug-report.md instead: first
line a title, then the location, a reproducer and the expected versus actual
result.

If every surviving mutant is equivalent, write that to .ai/skip-reason and stop.
