You are improving test coverage in $repo, unattended. Nobody will answer
questions. Your output is new or extended tests plus two files under .ai/.
A script checks your work, measures coverage again and opens the PR.

Target: `$target` is at $percent% line coverage. Uncovered lines:
$missing

1. Read CLAUDE.md if it exists, then the target file and its existing tests.
2. Write tests that exercise the uncovered lines through the public behaviour
   of the code: real inputs, meaningful assertions on outputs, errors raised
   for invalid inputs. Follow the existing test style, fixtures and
   hypothesis usage. Prefer a few thorough tests over many shallow ones.
   Don't write tests that only execute a line without checking anything.
3. Only add or change files under tests/ (including conftest.py). Never edit
   the code under test. If a line can only be covered by changing the code,
   leave it uncovered.
4. Run `.ai/ci-checks`, which runs every check CI runs. Anything that passed
   before your change must still pass. CI type-checks tests too: if a test
   deliberately passes input outside a type hint (a 1D array, another dtype),
   add a targeted `# ty: ignore[<rule>]` on that line saying why.
5. Write .ai/pr-title (one line, imperative, under 70 characters) and
   .ai/pr-body.md describing what behaviour the new tests pin down.

If writing a correct test shows the code is wrong (it crashes on valid input,
returns something the docstring says it shouldn't, and so on), don't write a
test that locks in the wrong behaviour. Describe the bug in
.ai/bug-report.md instead: first line a title, then the location, a minimal
reproducer and the expected versus actual result. Keep going with the rest of
the tests. The script files the bug as an issue.

If there is nothing worth testing (the lines are unreachable, or trivially
defensive), write the reason to .ai/skip-reason and stop.
