# meta-ci-action

Shared, reusable GitHub Actions CI workflow for `ucgmsim` Python repos. Runs
`ruff` (check + format), `deptry`, `numpydoc` lint, `pytest`, and `ty`
(type checking) as five parallel jobs.

## Usage

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
| `enable-coverage` | `false` | When true, runs pytest with `--cov=<cov-package>` and gates on `cov-fail-under`. |
| `cov-package` | `""` | Package name passed to `--cov=`. Required when `enable-coverage` is true. |
| `cov-fail-under` | `95` | Coverage percentage threshold passed to `coverage report --fail-under=`. |

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

