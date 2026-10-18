# ai-jobs

Scheduled issue work by Claude, as the org's GitHub App (`ucgmsim[bot]`), run by the
reusable workflow `.github/workflows/ai-jobs.yml`. Formerly the `issue-sweep` and `ai-ok`
Dagu DAGs in `dagu-workflow`, with the same logic, prompts and guardrails.

| Job | When (Auckland) | Output |
|---|---|---|
| `issue-sweep` | Sundays | Reviews ~2,500 lines not swept recently and files up to 5 vetted issues (`ai-suggested`; `ai-serious` for serious bugs, assigned to the reviewer) |
| `ai-ok` | daily | A PR fixing the oldest open issue a maintainer labelled `ai-ok`: a failing test first, then the fix. It must pass the tests and break no CI check. Giving up labels the issue `ai-stuck` |

Each repo has one caller workflow with a daily cron. Each job's log and Claude's
results are uploaded as the `ai-jobs-<job>` artifact. Each job's result also shows in the
run's summary.

## Setting up the org (once)

1. **GitHub App.** Reuse the existing bot app (`ucgmsim[bot]`), or create one in
   *Org settings → Developer settings → GitHub Apps → New GitHub App*: webhook off;
   repository permissions *Contents*, *Pull requests* and *Issues*: read and write; *Only on
   this account*. Note its **App ID** and generate a **private key**. Install it on every
   repo that runs the jobs. PRs have to come from the app, not `GITHUB_TOKEN`: PRs opened
   with `GITHUB_TOKEN` don't trigger the repo's CI.
2. **Org secrets** (*Org settings → Secrets and variables → Actions*), available to the
   repos that run the jobs:
   - `AI_JOBS_APP_ID`: the App ID;
   - `AI_JOBS_APP_PRIVATE_KEY`: the whole `.pem` file;
   - `CLAUDE_CODE_OAUTH_TOKEN`: from `claude setup-token` (the one `claude-review.yml`
     uses already).
3. **Actions policy**: if the org restricts which actions may run (*Org settings → Actions →
   General*), allow `actions/*` and `astral-sh/setup-uv`.

## Adding a repo

Add `.github/workflows/ai-jobs.yml`:

```yaml
name: AI jobs

on:
  schedule:
    - cron: "17 13 * * *"  # daily, ~02:17 in Auckland; the sweep runs on Sundays
  workflow_dispatch:
    inputs:
      jobs:
        description: "issue-sweep, ai-ok or both (empty: today's)"
        type: string
        default: ""
      live:
        description: "Open the PR / file the issues (off: only upload them)"
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
      # config: |
      #   sync = ["--all-extras"]
```

Then run it by hand once (*Actions → AI jobs → Run workflow*, jobs `issue-sweep`, live
off) and check the artifact for the issues it would file.

## Configuring a repo

`config` is TOML on top of [`aij/defaults.toml`](aij/defaults.toml), which documents every
key. The usual ones are `sync` (`uv sync` arguments, default `["--all-extras", "--dev"]`),
`test` (default `uv run --no-sync pytest tests -q -x`), `reviewer`, and `budgets.ai-ok` /
`budgets.issue-sweep` (USD per Claude run, default 20).

## Notes

- **Dry runs.** With live off, nothing is pushed or filed: the `ai-jobs-<job>` artifact holds
  the patch, the PR text or the issues. To publish, run the job again with live on.
- **Which files the sweep reviewed** is kept in the repo's Actions cache. If it's evicted,
  the sweep starts its rotation over.
- **Only maintainers' `ai-ok` labels count** (`maintainers` in defaults.toml), including
  over the sweep's own: for ai-ok to take an issue the sweep labelled, a maintainer removes
  and re-adds the label.
- **Inactive repos.** GitHub disables scheduled workflows in a public repo after 60 days
  without commits; re-enable it in the Actions tab.
- **Claude** runs with edit tools and test runners only. It gets no GitHub token: the
  workflow keeps the app key and tokens out of its environment, and the script does the
  pushing after its checks.
- **Changing the code.** `aij/jobs.py` holds each job's steps in order; the prompts are in
  `aij/prompts/`. Callers run the code at `meta-ref` (default `v2`), so a change reaches
  them when the `v2` tag moves.
