You are bringing the CI of $repo in line with the team's shared CI,
unattended. Nobody will answer questions. The goal is one PR that makes the
repo conformant AND green: the CI changes together with the fixes they need.
Your output is edits in the working tree plus two files under .ai/. A script
validates the result, may ask you to repair what still fails, and opens the PR.

The shared reusable workflows live in ucgmsim/meta-ci-action, checked out
read-only at $meta_dir. Read its .github/workflows/*.yml, lefthook/ci.yml and
README.md to see which jobs and inputs it offers.

$baseline

## Already done by the script (committed on this branch)

$done

## The shared CI's checks, run locally on this branch right now

`.ai/ci-checks` runs exactly what the shared CI runs (the same commands as
each ci.yml job). Its result before you start:

$gate

The GitHub CI status of the default branch, for context:
$ci_status

## What to do

Do all of these that apply, in one coherent change:
1. Make the repo's CI match the baseline: replace repo-local workflows that
   duplicate a meta-ci-action job with a call to the shared ci.yml@v2, pass
   inputs reproducing what the local workflows did (system packages, uv
   extras, coverage threshold, pytest paths), pin to v2, add the push trigger.
   Delete replaced workflow files by listing their paths (one per line) in
   .ai/delete; you have no rm. Don't disable them instead.
2. Move tool config into pyproject.toml where meta-ci-action reads it from:
   in particular, if the CI call passes `package-dir` or
   `numpydoc-extra-excludes`, move them to `[tool.npdlint]` `include` and
   `extend-exclude` and drop the inputs. The local npdlint check reads only
   `[tool.npdlint]`, so without this it lints the whole tree.
3. Fix every check that fails in `.ai/ci-checks`: type errors, docstring
   sections, clippy warnings the autofix couldn't handle, deptry findings.
   Fix the code or config properly. Rerun `.ai/ci-checks` until it passes.

Rules:
- Don't change dependency versions or uv.lock/Cargo.lock; the script restores
  the lockfiles anyway. Use `uv run --no-sync` so uv doesn't re-lock.
- Don't weaken any check: never lower a coverage threshold, skip tests, add
  blanket ignores, or drop a job. A targeted, commented ignore is fine only
  where the code is deliberately outside a rule (e.g. a test passing input
  outside a type hint on purpose).
- Keep repo-specific jobs that meta-ci-action has no equivalent for.
- Don't add a "Generated with Claude Code" footer; the script adds one.
- Write .ai/pr-title (one line, under 70 characters) and .ai/pr-body.md: what
  changed, which checks now run that didn't before, which failures you fixed
  and how, and anything that might still go red.

If there is genuinely nothing to change and `.ai/ci-checks` passes, write
"conformant" to .ai/skip-reason.
