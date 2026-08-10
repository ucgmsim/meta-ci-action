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
```

## Inputs

| Input | Default | Purpose |
|---|---|---|
| `python-version` | `"3.13"` | Interpreter version for the numpydoc job's `setup-python` step. |
| `package-dir` | *(required)* | Top-level package directory numpydoc lints, e.g. `qcore`, `workflow`, `IM`. |
| `system-packages` | `""` | Space-separated apt packages installed before the deptry/pytest/typecheck jobs (e.g. native libs like GMT or GDAL). |
| `uv-extra-args` | `"--all-extras --dev"` | Extra flags passed to `uv sync` in the deptry/pytest/typecheck jobs. |
| `numpydoc-extra-excludes` | `""` | Extra `-E` fdfind exclude fragments for numpydoc, e.g. `-E ccldpy.py`. `__init__.py` is always excluded. |

## Design principles

- **Args live in `pyproject.toml`, not the workflow.** Tool behavior (ruff
  rules, deptry dev-dependency groups, `ty` excludes) should be configured via
  each repo's own `[tool.*]` sections, so this workflow stays argument-free
  where possible. The two exceptions — `package-dir` and
  `numpydoc-extra-excludes` — exist because `numpydoc`'s CLI has no
  path-exclude equivalent expressible in `pyproject.toml`.
- **No coverage gating.** This workflow only runs `pytest tests`, with no
  `--cov` flags or coverage threshold. Repos that want coverage enforcement
  keep their own separate workflow for it.

## Dependabot

`.github/dependabot.yml` watches the `github-actions` ecosystem but is scoped
via `allow` to only `astral-sh/ruff-action` — other actions used in `ci.yml`
(`actions/checkout`, `astral-sh/setup-uv`, `actions/setup-python`,
`awalsh128/cache-apt-pkgs-action`) are intentionally left unmanaged so ruff
version bumps don't get lost in unrelated action-update noise.


