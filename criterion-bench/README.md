# criterion-bench

A composite action that benchmarks a Rust crate's [criterion](https://github.com/bheisler/criterion.rs)
benches on a PR's **base and head, back to back on the same runner**, and posts
one sticky PR comment with:

- a headline tally (🔴 slower · 🟢 faster · ⚪ no significant change · 🆕 new · 🗑️ removed)
  and a callout for the largest regression or win;
- a **change ridgeline**: the bootstrapped distribution of the relative change
  per benchmark, criterion's 95% CI as a bar, and the noise band shaded;
- a results table: base and PR typical time, change with its 95% CI, throughput;
- **distribution small multiples**: kernel density of time per iteration, base
  vs PR, with every sample as a rug tick and the medians marked;
- collapsed extras: **scaling** (log-log time vs parameter, for groups
  benchmarked at several numeric inputs) and **raw samples** in collection
  order (drift and noisy-neighbour check).

Every plot is rendered in a light and a dark variant and embedded with
`<picture>`, so it follows the reader's GitHub theme. See
[`example/comment.md`](example/comment.md) for a rendered comment.

## Usage

```yaml
name: Benchmarks
on:
  pull_request:

jobs:
  criterion:
    runs-on: ubuntu-latest
    permissions:
      contents: write        # push plot PNGs to the criterion-plots branch
      pull-requests: write   # post the comment
    steps:
      - uses: actions/checkout@v4
      - uses: ucgmsim/meta-ci-action/criterion-bench@v2
        # with:
        #   working-directory: gm_models   # crate not at the repo root
```

That is the whole integration: the action installs Rust, caches the build,
finds every `[[bench]]` target via `cargo metadata`, and handles the base
checkout itself. Nothing needs to change in the benches, beyond them being
`harness = false` criterion benches.

On a non-PR event (e.g. `push` to master) it benchmarks the head only and
writes the table and plots to the job summary.

## Inputs

| Input | Default | Purpose |
|---|---|---|
| `working-directory` | `.` | Directory holding the crate's `Cargo.toml`. |
| `benches` | all | Space-separated bench targets to run. |
| `cargo-args` | | Extra `cargo bench` args, e.g. `--features simd`. |
| `bench-args` | | Extra criterion args after `--`, e.g. `--sample-size 50`. |
| `threshold` | `5` | Noise threshold (%). A change counts only when its **whole** 95% CI lies beyond it. |
| `system-packages` | | apt packages to install first. |
| `fail-on-regression` | `false` | Fail the job when anything is slower beyond the threshold. |
| `plots-branch` | `criterion-plots` | Orphan branch the PNGs go to; `''` posts tables only. |
| `comment` | `true` | Post/update the sticky PR comment. |
| `title` | | Comment heading. |

Outputs: `regressions`, `improvements` (counts).

## How it works

1. `cargo bench --bench <each> -- --save-baseline base` in a worktree of the
   base commit, then `-- --baseline-lenient base` on the head. Both builds
   share the head's `target/`, and criterion writes to a temporary
   `CRITERION_HOME`.
2. `report.py plots` reads criterion's JSON output and renders PNGs;
   `report.py markdown` writes the comment.
3. PNGs are committed to the `criterion-plots` branch and linked by commit SHA
   (comments cannot carry image attachments through the API).
4. The comment is found by a hidden marker and edited in place.
5. Criterion's own HTML report is uploaded as the `criterion-<dir>` artifact.

From a fork PR the token is read-only, so neither the plots nor the comment
can be written; the job summary still gets the tables.

## What criterion writes, and what is plotted from it

Per benchmark, under `<criterion home>/<group>/<function>/<value>/`:

| File | Contents | Used for |
|---|---|---|
| `{base,new}/sample.json` | raw samples: `iters[i]` iterations took `times[i]` ns | distributions, raw samples, bootstrap ridgeline |
| `{base,new}/estimates.json` | bootstrapped mean, median, MAD, std dev, slope, each with a 95% CI | table, scaling |
| `{base,new}/tukey.json` | Tukey fences (outlier thresholds) | raw-samples band |
| `{base,new}/benchmark.json` | group/function/value ids, throughput | labels, throughput, scaling grouping |
| `change/estimates.json` | relative change in mean and median, with CI | verdicts, table, ridgeline bars |
| `report/` | criterion's own SVG plots and HTML | uploaded as an artifact, not re-plotted |

Further plots worth adding if they earn their place:

- **Flamegraph diff**: both nzcvm and Empirical-Engine already wire up
  `pprof`'s criterion profiler; running with `--profile-time` and uploading
  base/head flamegraphs (or an inferno differential flamegraph) would show
  *where* a regression lives.
- **History on master**: storing each master run's `estimates.json` (e.g. on
  the same plots branch) would allow a trend line per benchmark, catching
  slow drifts no single PR crosses the threshold for.
- **MAD / dispersion change**: `median_abs_dev` is already in
  `estimates.json`; a column flagging benchmarks that got noisier would catch
  regressions in tail latency that leave the mean alone.
