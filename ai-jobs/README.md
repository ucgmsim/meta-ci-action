# ai-jobs

Scheduled repo maintenance by Claude, as the org's GitHub App (`ucgmsim[bot]`), run by the
reusable workflow `.github/workflows/ai-jobs.yml`. Formerly the Dagu DAGs in
`dagu-workflow`; the job logic, prompts, limits and guardrails are the same.

| Job | When (Auckland) | Output |
|---|---|---|
| `ai-ok` | daily | PR for the oldest `ai-ok` issue a maintainer labelled; giving up labels it `ai-stuck` |
| `lockfile` | Sunday | One `ai/lockfile` PR refreshing `uv.lock`/`Cargo.lock`; closes lockfile-only Dependabot PRs |
| `lint` | Sunday | One `ai/lint` PR with ruff's safe fixes |
| `ci-conformance` | Sunday | One PR moving the repo's CI onto `meta-ci-action@v2` |
| `issue-sweep` | Sunday | Up to 5 vetted issues |
| `coverage` | Wednesday | Tests-only PR for the least-covered file |
| `mutants` | Wednesday | Tests-only PR killing surviving mutants |
| `ci-promote` | Sunday, meta-ci-action only | One PR promoting CI several repos duplicate into meta-ci-action |

Each repo has one caller workflow with a daily cron. The first job works out which jobs are
due today, then they run one at a time, each on a fresh runner. Every job's log and
Claude's results are uploaded as the `ai-jobs-<job>` artifact. Each job's result also
shows in the run's summary.

## Setting up the org (once)

1. **GitHub App.** Reuse the existing bot app (`ucgmsim[bot]`), or create one in
   *Org settings → Developer settings → GitHub Apps → New GitHub App*: webhook off;
   repository permissions *Contents*, *Pull requests*, *Issues* and *Workflows*: read and
   write, *Actions*: read-only; *Only on this account*. Note its **App ID** and generate a
   **private key**. Install it on meta-ci-action and on every repo that runs the jobs.
   PRs have to come from the app, not `GITHUB_TOKEN`: PRs opened with `GITHUB_TOKEN`
   don't trigger the repo's CI.
2. **Org secrets** (*Org settings → Secrets and variables → Actions*), available to the
   repos that run the jobs:
   - `AI_JOBS_APP_ID`: the App ID;
   - `AI_JOBS_APP_PRIVATE_KEY`: the whole `.pem` file;
   - `CLAUDE_CODE_OAUTH_TOKEN`: from `claude setup-token` (the one `claude-review.yml`
     uses already).
3. **Actions policy** (*Org settings → Actions → General*): allow the actions the
   workflow uses: `actions/*`, `astral-sh/setup-uv`, `taiki-e/install-action`.

## Adding a repo

Add `.github/workflows/ai-jobs.yml`:

```yaml
name: AI jobs

on:
  schedule:
    - cron: "17 13 * * *"  # daily, ~02:17 in Auckland; the weekly jobs run on their day
  workflow_dispatch:
    inputs:
      jobs:
        description: "Jobs to run, space-separated (empty: today's)"
        type: string
        default: ""
      live:
        description: "Push and open PRs (off: only upload the patches)"
        type: boolean
        default: false
      issue:
        description: "ai-ok: try this issue number regardless of labels"
        type: string
        default: "0"

concurrency:
  group: ai-jobs-${{ github.repository }}

permissions:
  contents: read

jobs:
  ai-jobs:
    uses: ucgmsim/meta-ci-action/.github/workflows/ai-jobs.yml@v2
    secrets: inherit
    with:
      jobs: ${{ inputs.jobs }}
      live: ${{ github.event_name == 'schedule' || inputs.live }}
      issue: ${{ inputs.issue }}
      system-packages: ""  # as in the repo's ci.yml
      config: |
        cov_package = "mypackage"
        mut_python = ["mypackage"]
```

Then run it by hand once (*Actions → AI jobs → Run workflow*, jobs `lint`, live off) and
check the log: it should clone, `uv sync`, run the CI gate and the tests.

## Configuring a repo

`config` is TOML on top of [`aij/defaults.toml`](aij/defaults.toml), which documents every
key. Tables (`budgets`, `timeouts`, `diff_limits`, `models`) merge key by key. The usual ones:

| Key | Default | |
|---|---|---|
| `sync` | `["--all-extras", "--dev"]` | `uv sync` arguments |
| `test` | `uv run --no-sync pytest tests -q -x` | The test command |
| `pytest_paths` | `["tests"]` | Where coverage and mutmut run pytest |
| `ruff_format` | `false` | `lint` also formats (when CI checks formatting) |
| `ruff_latest` | `false` | `lint` uses the latest ruff (CI runs an unpinned ruff) |
| `cov_package` | `""` | Package to measure; empty turns `coverage` off |
| `mut_python` | `[]` | Directories `mutants` mutates |
| `mut_rust` | unset | `{ package = "core", dir = "src-rust" }` to mutate a crate too |
| `reviewer` | `lispandfound` | Asked to review bot PRs, assigned `ai-serious` issues |
| `default` | the repo's default branch | Branch the jobs target |
| `budgets.<job>` | 20 | USD per Claude run |

To stop a job in a repo, give it nothing to do (e.g. `cov_package = ""`), or disable the
workflow in the repo's Actions tab to stop them all.

## Notes

- **Dry runs.** With live off, nothing is pushed or filed: the `ai-jobs-<job>` artifact holds
  the patch, the PR text and any issues. To publish, run the job again with live on.
- **Rotation state** (which files `coverage`, `mutants` and `issue-sweep` did recently) is
  kept in the repo's Actions cache. If it's evicted, the rotation starts over.
- **Limits.** A GitHub-hosted job runs for at most 6 hours.
- **Inactive repos.** GitHub disables scheduled workflows in a public repo after 60 days
  without commits; re-enable it in the Actions tab.
- **Claude** runs with edit tools and test runners only. It gets no GitHub token: the
  workflow keeps the app key and tokens out of its environment, and the script does the
  pushing after its checks.
- **Changing the code.** `aij/jobs.py` holds each job's steps in order; the prompts are in
  `aij/prompts/`. Callers run the code at `meta-ref` (default `v2`), so a change reaches
  them when the `v2` tag moves.
