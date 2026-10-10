# meta-ci-action

Shared, reusable GitHub Actions workflows for `ucgmsim` Python repos:

- **`ci.yml`** — `ruff` (check + format), `deptry`, `npdlint`
  (numpydoc-style docstrings), `pytest`, and `ty` (type checking) as five
  parallel jobs, plus a `rust` job (`cargo fmt`/`clippy`/`test`) that only
  appears for repos containing a `Cargo.toml`.
- **`claude-review.yml`** — an on-demand Claude PR review, triggered by a
  `@claude review` comment (never automatically on PR open/push).
- **`build-dist.yml`** — builds a release's sdist and wheels (one pure wheel,
  or a `cibuildwheel` matrix for repos with a `Cargo.toml`) as a single
  artifact, for the caller's PyPI publish job to upload.
- **`wiki.yml`** — publishes the repo's `wiki/` directory to its GitHub wiki,
  rewriting relative `.md` links to wiki page links on the way.

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
      # package-dir: qcore          # unset once [tool.npdlint] has `include`
      # system-packages: "gmt libgmt-dev ghostscript"
      # uv-extra-args: "--all-groups --all-extras"
      # exclude-init: false          # lint __init__.py too
      # enable-coverage: true
      # cov-package: qcore
      # cov-fail-under: 95
```

## Inputs

| Input | Default | Purpose |
|---|---|---|
| `python-version` | `"3.13"` | Interpreter version for the docstring job's `setup-python` step. It parses the code under test, so it must be at least as new as the syntax that code uses. |
| `package-dir` | `""` | Top-level package directory to lint for docstrings, e.g. `qcore`, `workflow`, `IM`. No longer required: leave it unset once `[tool.npdlint] include` names the same paths. |
| `system-packages` | `""` | Space-separated apt packages installed before the deptry/pytest/typecheck jobs (e.g. native libs like GMT or GDAL). |
| `uv-extra-args` | `"--all-extras --dev"` | Extra flags passed to `uv sync` in the deptry/pytest/typecheck jobs. |
| `numpydoc-extra-excludes` | `""` | **Deprecated.** Extra `-E` fdfind exclude fragments, e.g. `-E ccldpy.py`. Still honoured — each becomes an `npdlint --extend-exclude` — so a caller written for the old pipeline needs no edit, but `[tool.npdlint] extend-exclude` is the place for these now. |
| `exclude-init` | `true` | Skip `__init__.py` in the docstring job, as the fdfind pipeline always did. Set `false` once `[tool.npdlint]` owns the exclusions and you want `__init__.py` linted too. |
| `pytest-paths` | `"tests"` | Paths passed to `pytest`. Set to `""` to fall through to `[tool.pytest.ini_options] testpaths`. |
| `rust-features` | `""` | Space-separated cargo feature sets; each gets its own `cargo test --features <set>` run. Ignored when the repo has no `Cargo.toml`. |
| `rust-test-release` | `false` | Also run `cargo test --release` in the `rust` job. Debug and release builds can disagree under optimization, so a crate whose compiled extension does numeric work may want both profiles tested. Roughly doubles the `rust` job's runtime, so it stays opt-in. |
| `enable-coverage` | `false` | When true, runs pytest with `--cov=<cov-package>` and gates on `cov-fail-under`. |
| `cov-package` | `""` | Package name passed to `--cov=`. Required when `enable-coverage` is true. |
| `cov-fail-under` | `95` | Coverage percentage threshold passed to `coverage report --fail-under=`. |

## Rust / maturin repos

Nothing needs enabling. A `detect` job checks the repo out and looks for a
`Cargo.toml`; when it finds one:

- a `rust` job runs `cargo fmt --all --check`, `cargo clippy --all-targets -- -D
  warnings`, `cargo test`, `cargo test --doc`, and one `cargo test --features
  <set>` per entry in `rust-features`; set `rust-test-release: true` to also
  run `cargo test --release` — useful if the crate does numeric work that
  optimization can change the behavior of (e.g. LLVM folding `powf(x, 0.5)`
  into `sqrt(x)` at `-O2` but not at `-O0`), since debug-only testing would
  miss a regression that only shows up in the release profile actually
  shipped;
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

## Usage: run CI locally with lefthook

`lefthook/ci.yml` mirrors `ci.yml` as [lefthook](https://lefthook.dev) git
hooks, so a failure shows up on `git push` instead of in the PR. Each pre-push
command runs the same thing as the matching CI job:

| Hook command | CI job | Tag |
|---|---|---|
| `ruff`, `ruff-format` | `ruff` (at the ruff version pinned in `uv.lock`) | `ruff` |
| `deptry` | `deptry` | `deptry` |
| `npdlint` | `numpydoc-lint` (scope from `[tool.npdlint]`) | `docs` |
| `ty` | `typecheck` | `types` |
| `pytest` | `pytest`, without the coverage gate | `tests` |
| `rustfmt`, `clippy`, `cargo-test` | `rust` | `rust` |

Pre-commit runs, in order, the autofixes on the staged files (`ruff check
--fix`, `ruff format`, `cargo fmt`, staged back automatically) and then `ty
check`. It reports whatever ruff and ty can't fix but never blocks the commit;
pre-push is the gate.

In the consuming repo, a `lefthook.yml` that loads it:

```yaml
remotes:
  - git_url: https://github.com/ucgmsim/meta-ci-action
    ref: v2
    configs:
      - lefthook/ci.yml

# Optional: turn off commands by tag, e.g. slow tests on every push.
# pre-push:
#   exclude_tags: [tests]
```

Then, once per clone:

```bash
uv sync --all-extras --dev   # the hooks use the project environment, as CI does
uvx lefthook install
```

Notes:

- **Overriding.** lefthook gives remote configs priority, so redefining a
  command with the same name in the repo's `lefthook.yml` has no effect.
  Turn commands off with `exclude_tags`, or for one push with
  `LEFTHOOK_EXCLUDE=tests git push`. Commands with new names (e.g. a
  `yamllint` hook) merge in alongside.
- **Run the whole gate without pushing** (what the scheduled bot jobs do):
  `uvx lefthook run pre-push --all-files --no-tty`, or one check with
  `--command ty`. Don't add `--force`: it runs commands whose glob matches
  nothing, such as the Rust checks in a repo with no crate, which CI skips.
- **Not mirrored:** the coverage threshold (it needs `cov-package`),
  `rust-features`, `rust-test-release`, and `system-packages`. Install native
  libraries yourself.
- **Updates.** lefthook caches the remote. Pull a newer `v2` with
  `uvx lefthook install --force`, or set `refetch: true` on the remote.

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

## Usage: building distributions for PyPI

`build-dist.yml` replaces the `build` job(s) of a repo's `publish-PyPI.yml`.
It does **not** upload: `pypa/gh-action-pypi-publish` does not support
trusted publishing from inside a reusable workflow, so the PyPI upload stays
a short job in the caller, which also keeps the `pypi` environment and the
trusted-publisher configuration on PyPI unchanged. A whole
`.github/workflows/publish-PyPI.yml`:

```yaml
name: Publish to PyPI
run-name: Publish Python Package to PyPI for release ${{ github.event.release.tag_name || inputs.tag_name || github.ref_name }}

on:
  release:
    types: [published]
  workflow_dispatch:
    inputs:
      tag_name:
        description: "Git tag to checkout and publish"
        required: false
        type: string

jobs:
  build:
    uses: ucgmsim/meta-ci-action/.github/workflows/build-dist.yml@v2
    with:
      ref: ${{ inputs.tag_name }}       # empty on a release: builds the release tag
      # cibw-skip: "pp* *-musllinux_* *t-*"
      # cibw-build: "cp312-* cp313-* cp314-*"

  publish-to-pypi:
    name: Publish to PyPI
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: pypi
      url: https://pypi.org/p/<your-project>
    permissions:
      id-token: write
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: ${{ needs.build.outputs.artifact-name }}
          path: dist/
      - uses: pypa/gh-action-pypi-publish@v1.13.0
```

What it builds depends on the same `Cargo.toml` detection as `ci.yml`:

- **No crate:** one `ubuntu-latest` job runs `uv build`, giving a
  `py3-none-any` wheel and the sdist. No matrix, since the one wheel serves
  every platform.
- **A crate:** the sdist comes from `uv build --sdist`, and a
  `pypa/cibuildwheel` matrix over `wheel-runners` builds the platform wheels,
  with Rust installed on the runner (plus both macOS targets) and inside the
  manylinux container.

Checkouts use `fetch-depth: 0`, so setuptools-scm can read the version from
the tag. The `version` output is that version, read off the sdist filename,
for later jobs that need it (e.g. tagging a container image).

| Input | Default | Purpose |
|---|---|---|
| `ref` | `""` | Ref to check out and build. Empty means the ref that triggered the caller (the release tag on `release`). Pass a `workflow_dispatch` `tag_name` straight through. |
| `python-version` | `"3.13"` | Interpreter `uv build` runs the build backend with. |
| `artifact-name` | `"python-package-distributions"` | Name of the merged artifact; also the `artifact-name` output. |
| `wheel-runners` | `'["ubuntu-latest", "macos-latest", "windows-latest"]'` | JSON list of runners for the cibuildwheel matrix. Crate repos only. |
| `cibw-build` | `""` | `CIBW_BUILD`. Empty: every CPython `requires-python` allows. |
| `cibw-skip` | `"pp* *-musllinux_*"` | `CIBW_SKIP`. |
| `cibw-archs-linux` | `"x86_64"` | `CIBW_ARCHS_LINUX`. |
| `cibw-archs-macos` | `"x86_64 arm64"` | `CIBW_ARCHS_MACOS` (Intel and Apple Silicon). |
| `cibw-archs-windows` | `"AMD64"` | `CIBW_ARCHS_WINDOWS`. |
| `macosx-deployment-target` | `"11.0"` | `MACOSX_DEPLOYMENT_TARGET`; Rust needs at least 11.0 for the arm64 slice. |

The `cibw-*` inputs are exported as environment variables, which take
precedence over `[tool.cibuildwheel]` in `pyproject.toml`. To keep a setting
in `pyproject.toml` instead, pass `''` for that input; empty inputs are not
exported. There is no lefthook command for this workflow: it only builds
release artifacts, so there is nothing to check before a push.

## Usage: wiki sync

`wiki.yml` replaces a repo's own `wiki.yml`/`deploy-wiki.yml`. It checks the
repo out, strips the `.md` from relative links (`[Usage](Usage.md)` becomes
`[Usage](Usage)`, so links that work when browsing `wiki/` in the repo also
work in the wiki), and pushes the directory with `actions4gh/deploy-wiki`.
The trigger stays in the caller, and so does the `contents: write` grant,
since a called workflow cannot widen its caller's token permissions. A whole
`.github/workflows/wiki.yml`:

```yaml
name: deploy-wiki
on:
  push:
    branches: [main]          # the default branch
    paths: wiki/**
  workflow_dispatch:

jobs:
  deploy-wiki:
    uses: ucgmsim/meta-ci-action/.github/workflows/wiki.yml@v2
    permissions:
      contents: write
    # with:
    #   wiki-dir: docs/wiki
    #   rewrite-md-links: false
```

The wiki must already exist: create its first page once in the GitHub UI, or
`<repo>.wiki.git` has nothing to push to. Runs are queued per repo rather than
run in parallel, since each one replaces the whole wiki.

| Input | Default | Purpose |
|---|---|---|
| `wiki-dir` | `"wiki"` | Directory holding the wiki's Markdown pages. Its contents replace the wiki's. |
| `rewrite-md-links` | `true` | Strip `.md` from relative Markdown links before deploying. Absolute `https://` links are left alone. Set `false` if the pages already use wiki-style links. |

There is no lefthook command for this workflow: it only publishes, so there
is nothing to check before a push.

## Design principles

- **Args live in `pyproject.toml`, not the workflow.** Tool behavior (ruff
  rules, deptry dev-dependency groups, `ty` excludes) should be configured via
  each repo's own `[tool.*]` sections, so this workflow stays argument-free
  where possible. `package-dir` and `numpydoc-extra-excludes` used to be the
  exceptions, because `numpydoc`'s CLI could express neither a set of paths to
  walk nor a path to skip. `npdlint` reads both from `[tool.npdlint]`, so they
  are now optional and on their way out: a fully migrated repo calls this
  workflow with no docstring inputs at all.
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
2. **Identify what can't move to config.** Very little has to stay a `with:`
   input now. Docstring paths and excludes belong in `[tool.npdlint]`:

   ```toml
   [tool.npdlint]
   include = ["qcore"]
   extend-exclude = ["**/__init__.py", "ccldpy.py"]
   ```

   With that in place, drop `package-dir` and `numpydoc-extra-excludes` from
   the `ci.yml` call and set `exclude-init: false`, so the exclusions live in
   one file rather than two.
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

