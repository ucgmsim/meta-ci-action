# meta-ci-action

Shared, reusable GitHub Actions workflows for `ucgmsim` Python repos:

- **`ci.yml`** — `ruff` (check + format), `deptry`, `numpydoc` lint, `pytest`,
  and `ty` (type checking) as five parallel jobs, plus a `rust` job
  (`cargo fmt`/`clippy`/`test`) that only appears for repos containing a
  `Cargo.toml`.
- **`claude-review.yml`** — an on-demand Claude PR review, triggered by a
  `@claude review` comment (never automatically on PR open/push).

## Usage: CI

In the consuming repo, replace the repo's own `ruff.yml`, `deptry.yml`,
`numpydoc.yml`, `pytest.yml`, and `types.yml` workflows with a single file,
e.g. `.github/workflows/ci.yml`:

```yaml
name: CI
on: [pull_request]

jobs:
  ci:
    uses: ucgmsim/meta-ci-action/.github/workflows/ci.yml@v1
    with:
      package-dir: qcore
      # system-packages: "gmt libgmt-dev ghostscript"
      # uv-extra-args: "--all-groups --all-extras"
      # numpydoc-extra-excludes: "-E ccldpy.py"
      # enable-coverage: true
      # cov-package: qcore
      # cov-fail-under: 95
```

## Inputs

| Input | Default | Purpose |
|---|---|---|
| `python-version` | `"3.13"` | Interpreter version for the numpydoc job's `setup-python` step. |
| `package-dir` | *(required)* | Top-level package directory numpydoc lints, e.g. `qcore`, `workflow`, `IM`. |
| `system-packages` | `""` | Space-separated apt packages installed before the deptry/pytest/typecheck jobs (e.g. native libs like GMT or GDAL). |
| `uv-extra-args` | `"--all-extras --dev"` | Extra flags passed to `uv sync` in the deptry/pytest/typecheck jobs. |
| `numpydoc-extra-excludes` | `""` | Extra `-E` fdfind exclude fragments for numpydoc, e.g. `-E ccldpy.py`. `__init__.py` is always excluded. |
| `pytest-paths` | `"tests"` | Paths passed to `pytest`. Set to `""` to fall through to `[tool.pytest.ini_options] testpaths`. |
| `rust-features` | `""` | Space-separated cargo feature sets; each gets its own `cargo test --features <set>` run. Ignored when the repo has no `Cargo.toml`. |
| `enable-coverage` | `false` | When true, runs pytest with `--cov=<cov-package>` and gates on `cov-fail-under`. |
| `cov-package` | `""` | Package name passed to `--cov=`. Required when `enable-coverage` is true. |
| `cov-fail-under` | `95` | Coverage percentage threshold passed to `coverage report --fail-under=`. |

## Rust / maturin repos

Nothing needs enabling. A `detect` job checks the repo out and looks for a
`Cargo.toml`; when it finds one:

- a `rust` job runs `cargo fmt --all --check`, `cargo clippy --all-targets -- -D
  warnings`, `cargo test`, `cargo test --doc`, and one `cargo test --features
  <set>` per entry in `rust-features`;
- the `deptry`, `pytest` and `typecheck` jobs additionally install a stable
  Rust toolchain and a `Swatinem/rust-cache@v2` cache, since `uv sync` has to
  build the extension module in each of them.

For a maturin project, put the build profile in `uv-extra-args` — otherwise
every Python job compiles the crate with the release profile, which for a
crate using `lto`/`codegen-units = 1` dominates the CI time:

```yaml
jobs:
  ci:
    uses: ucgmsim/meta-ci-action/.github/workflows/ci.yml@main
    with:
      package-dir: nzcvm
      uv-extra-args: "--all-extras --dev -C build-args=--profile=dev"
      rust-features: "high_precision"
      pytest-paths: ""
```

Each job runs `uv sync` once and then `uv run --no-sync`, so the config
settings applied at sync time are the ones the tests run against and no step
silently rebuilds the crate.

## Usage: Claude PR review

`claude-review.yml` is a `workflow_call`-only reusable workflow — it has no
trigger of its own. Each consuming repo needs its own thin wrapper carrying
the actual `issue_comment` trigger, e.g. `.github/workflows/claude-review.yml`:

```yaml
name: Claude PR Review

on:
  issue_comment:
    types: [created]

jobs:
  review:
    uses: ucgmsim/meta-ci-action/.github/workflows/claude-review.yml@v1
    secrets:
      claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

The reusable workflow itself gates on `contains(github.event.comment.body,
'@claude review')` and only runs on PR comments — it does not run on PR
creation, push, or any other event. `CLAUDE_CODE_OAUTH_TOKEN` must be set as
a secret in the consuming repo (or its org) and passed through explicitly,
since reusable workflows don't inherit secrets automatically unless declared.

## Design principles

- **Args live in `pyproject.toml`, not the workflow.** Tool behavior (ruff
  rules, deptry dev-dependency groups, `ty` excludes) should be configured via
  each repo's own `[tool.*]` sections, so this workflow stays argument-free
  where possible. The two exceptions — `package-dir` and
  `numpydoc-extra-excludes` — exist because `numpydoc`'s CLI has no
  path-exclude equivalent expressible in `pyproject.toml`.
- **Coverage is opt-in.** Set `enable-coverage: true` (plus `cov-package`) to
  add a `--cov` run and a `coverage report --fail-under=` gate to the pytest
  job. Left `false`, pytest just runs `pytest tests` with no coverage
  instrumentation at all — matches repos that don't want the extra CI time
  or don't have a coverage target yet.

## Migrating a repo to this workflow

1. **Move hardcoded CLI args into `pyproject.toml`.** Check the repo's
   existing `ruff.yml`/`deptry.yml`/`types.yml`/etc. for flags baked into the
   `run:` steps (e.g. `ty --exclude`, `deptry`'s dev-dependency groups) and
   move them into the matching `[tool.*]` section instead, so the shared
   workflow can invoke each tool without repo-specific arguments.
2. **Identify what can't move to config.** A few things (like numpydoc's
   path excludes) have no `pyproject.toml` equivalent — these stay as
   `with:` inputs on the `ci.yml` call.
3. **Add the caller workflow.** Create `.github/workflows/ci.yml` in the
   consuming repo per the [Usage](#usage) example above, setting only the
   inputs that differ from the defaults.
4. **Delete the superseded workflows** (whichever of `ruff.yml`,
   `deptry.yml`, `numpydoc.yml`, `pytest.yml`, `types.yml` the repo has).
   Leave anything unrelated to these five tools untouched.
5. **Validate before merging.** Open a draft PR and confirm each job passes
   (or fails the same way the old workflow did) before relying on it.

Adopting `claude-review.yml` is independent of the above — it's a separate
opt-in workflow, not part of the `ci.yml` migration.

