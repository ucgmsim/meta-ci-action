## CI baseline for ucgmsim repos

Every Python repo's CI must:
- call `ucgmsim/meta-ci-action/.github/workflows/ci.yml` for lint, format,
  docstrings, deptry, type checking and tests, instead of per-tool local
  workflows;
- pin meta-ci-action to the `v2` tag, never `@main`, so changes on main are
  tested before they reach every repo;
- enable coverage (`enable-coverage: true`) with `cov-fail-under` at least
  95, unless the repo documents why not;
- run on `pull_request` and on `push` to the default branch;
- call meta-ci-action's `claude-review.yml` from a thin `issue_comment` wrapper
  if the repo wants on-demand Claude review.
- keep its `ai-jobs.yml` caller of meta-ci-action's `ai-jobs.yml` (the
  scheduled bot jobs, including this one) as it is.

Every repo's git hooks must:
- come from a `lefthook.yml` at the repo root that loads meta-ci-action's
  `lefthook/ci.yml` as a remote pinned to `v2`, so the pre-push hook runs the
  same checks as CI:

  ```yaml
  remotes:
    - git_url: https://github.com/ucgmsim/meta-ci-action
      ref: v2
      configs:
        - lefthook/ci.yml
  ```

- not redefine commands the shared config already has (pre-commit:
  ruff-fix, ruff-format, rustfmt, ty; pre-push: ruff, ruff-format, deptry,
  npdlint, ty, pytest, rustfmt, clippy, cargo-test): lefthook gives the
  remote priority, so a local copy is dead code. Turn shared commands off
  with `exclude_tags` instead;
- keep repo-specific extras (for example a `yamllint` hook) as commands with
  their own names, which merge in alongside the shared ones.

Third-party actions must:
- use a current major version (for example `actions/checkout@v4` or later,
  `actions/setup-python@v5` or later);
- never be pinned to `@latest` or a branch;
- not be deprecated or archived (for example use `astral-sh/ruff-action`, not
  `chartboost/ruff-action`).

Checks must be green on the default branch. A red check on the default
branch is a bug to fix, not to silence.
