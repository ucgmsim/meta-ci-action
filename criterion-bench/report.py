"""Render a criterion.rs base-vs-PR comparison as plots and a PR comment.

Criterion writes one directory per benchmark under its output directory
(``$CRITERION_HOME``, by default ``target/criterion``)::

    <bench dir>/base/{benchmark,estimates,sample,tukey}.json   # base branch
    <bench dir>/new/{benchmark,estimates,sample,tukey}.json    # this PR
    <bench dir>/change/estimates.json                          # relative change

``sample.json`` holds the raw measurements (``iters[i]`` iterations took
``times[i]`` ns in total), which is all the distribution plots need;
``estimates.json`` holds criterion's bootstrapped point estimates and 95%
confidence intervals, which the tables and the verdicts use.

Two subcommands, so the plots can be published before the markdown that
links to them is written:

``plots``     read the criterion directory, write PNGs (light and dark
              variants of each) and ``summary.json`` to ``--out``.
``markdown``  read the criterion directory, write the comment body to
              stdout, linking images under ``--image-base`` (omit it to
              produce a text-only comment).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


@dataclass
class Run:
    """One side (base or PR) of one benchmark."""

    estimates: dict
    per_iter: np.ndarray  # ns per iteration, one value per sample
    tukey: list[float] | None

    @property
    def typical(self) -> dict:
        """Criterion's headline estimate: the slope for linear sampling."""
        return self.estimates.get("slope") or self.estimates["mean"]


@dataclass
class Bench:
    """A benchmark seen in the base run, the PR run, or both."""

    id: str
    group: str
    function: str | None
    value: str | None
    throughput: dict | None
    base: Run | None = None
    new: Run | None = None
    change: dict | None = None  # change/estimates.json
    verdict: str = field(default="")

    @property
    def change_mean(self) -> dict | None:
        return self.change["mean"] if self.change else None


def _read_json(path: Path):
    return json.loads(path.read_text())


def _load_run(directory: Path) -> Run | None:
    if not (directory / "estimates.json").exists():
        return None
    sample = _read_json(directory / "sample.json")
    per_iter = np.asarray(sample["times"], float) / np.asarray(sample["iters"], float)
    tukey_path = directory / "tukey.json"
    tukey = _read_json(tukey_path) if tukey_path.exists() else None
    return Run(_read_json(directory / "estimates.json"), per_iter, tukey)


def _natural_key(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def load(criterion_dir: Path, base_name: str, new_name: str) -> list[Bench]:
    """Collect every benchmark that has data in either baseline."""
    benches: dict[Path, Bench] = {}
    for meta_path in criterion_dir.rglob("benchmark.json"):
        side = meta_path.parent
        if side.name not in (base_name, new_name):
            continue
        bench_dir = side.parent
        if bench_dir in benches:
            continue
        meta = _read_json(meta_path)
        bench = Bench(
            id=meta["full_id"],
            group=meta["group_id"],
            function=meta.get("function_id"),
            value=meta.get("value_str"),
            throughput=meta.get("throughput"),
            base=_load_run(bench_dir / base_name),
            new=_load_run(bench_dir / new_name),
        )
        change_path = bench_dir / "change" / "estimates.json"
        if bench.base and bench.new and change_path.exists():
            bench.change = _read_json(change_path)
        benches[bench_dir] = bench
    return sorted(benches.values(), key=lambda b: _natural_key(b.id))


def classify(benches: list[Bench], threshold: float) -> None:
    """Set each benchmark's verdict from its change confidence interval.

    A change counts only when the whole 95% interval of the relative change
    in mean time sits beyond ``threshold``: CI runners are noisy, and
    criterion's own 1% default flags shared-runner jitter as a regression.
    """
    for b in benches:
        if b.new and not b.base:
            b.verdict = "new"
        elif b.base and not b.new:
            b.verdict = "removed"
        elif b.change is None:
            b.verdict = "new" if b.new else "removed"
        else:
            ci = b.change_mean["confidence_interval"]
            if ci["lower_bound"] > threshold:
                b.verdict = "slower"
            elif ci["upper_bound"] < -threshold:
                b.verdict = "faster"
            else:
                b.verdict = "unchanged"


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #

_TIME_UNITS = [("s", 1e9), ("ms", 1e6), ("µs", 1e3), ("ns", 1.0)]


def time_unit(ns: float) -> tuple[str, float]:
    """Largest unit in which ``ns`` is at least 1."""
    for name, scale in _TIME_UNITS:
        if ns >= scale:
            return name, scale
    return "ps", 1e-3


def fmt_time(ns: float) -> str:
    unit, scale = time_unit(ns)
    value = ns / scale
    digits = 0 if value >= 100 else 1 if value >= 10 else 2
    return f"{value:.{digits}f} {unit}"


def fmt_pct(frac: float) -> str:
    return f"{frac * 100:+.1f}%"


def fmt_throughput(throughput: dict | None, ns: float) -> str:
    if not throughput:
        return ""
    kind, amount = next(iter(throughput.items()))
    per_sec = amount / (ns * 1e-9)
    if kind in ("Bytes", "BytesDecimal"):
        base = 1024 if kind == "Bytes" else 1000
        units = ["B", "KiB", "MiB", "GiB", "TiB"] if base == 1024 else ["B", "kB", "MB", "GB", "TB"]
        suffix = "/s"
    else:
        base, units, suffix = 1000, ["", "K", "M", "G", "T"], " elem/s"
    i = 0
    while per_sec >= base and i < len(units) - 1:
        per_sec /= base
        i += 1
    return f"{per_sec:.3g} {units[i]}{suffix}".replace("  ", " ")


VERDICT_ICON = {
    "slower": "🔴",
    "faster": "🟢",
    "unchanged": "⚪",
    "new": "🆕",
    "removed": "🗑️",
}
VERDICT_LABEL = {
    "slower": "slower",
    "faster": "faster",
    "unchanged": "no significant change",
    "new": "new",
    "removed": "removed",
}

# --------------------------------------------------------------------------- #
# Plotting
# --------------------------------------------------------------------------- #

# Reference data-viz palette. Base is recessive grey ink, the PR is categorical
# slot 1 (blue), and verdicts use the fixed status colours; every verdict is
# also spelled out in text and icon in the table, so colour never carries it
# alone.
THEMES = {
    "light": {
        "ink": "#0b0b0b",
        "ink2": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "band": "#f0efec",
        "base": "#898781",
        "pr": "#2a78d6",
        "slower": "#d03b3b",
        "faster": "#0ca30c",
        "unchanged": "#898781",
    },
    "dark": {
        "ink": "#f0efec",
        "ink2": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2a2a2e",
        "axis": "#383835",
        "band": "#26262a",
        "base": "#898781",
        "pr": "#3987e5",
        "slower": "#d03b3b",
        "faster": "#0ca30c",
        "unchanged": "#898781",
    },
}


def _style(plt, theme: dict) -> None:
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.size": 9,
            "text.color": theme["ink"],
            "axes.labelcolor": theme["ink2"],
            "axes.edgecolor": theme["axis"],
            "axes.facecolor": "none",
            "axes.titlecolor": theme["ink"],
            "axes.titlesize": 9.5,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.spines.left": False,
            "axes.grid": True,
            "grid.color": theme["grid"],
            "grid.linewidth": 0.6,
            "xtick.color": theme["muted"],
            "ytick.color": theme["muted"],
            "xtick.labelcolor": theme["ink2"],
            "ytick.labelcolor": theme["ink2"],
            "legend.frameon": False,
            "figure.facecolor": "none",
            "savefig.facecolor": "none",
            "savefig.transparent": True,
        }
    )


def kde(samples: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Gaussian kernel density estimate with Silverman's bandwidth."""
    samples = samples[np.isfinite(samples)]
    n = samples.size
    std = samples.std(ddof=1) if n > 1 else 0.0
    iqr = np.subtract(*np.percentile(samples, [75, 25])) if n > 1 else 0.0
    spread = min(std, iqr / 1.34) if iqr > 0 else std
    bandwidth = 0.9 * spread * n ** (-1 / 5) if spread > 0 else max(abs(samples.mean()) * 1e-3, 1e-12)
    z = (grid[:, None] - samples[None, :]) / bandwidth
    return np.exp(-0.5 * z * z).sum(axis=1) / (n * bandwidth * math.sqrt(2 * math.pi))


def bootstrap_change(base: np.ndarray, new: np.ndarray, n_resamples: int = 4000) -> np.ndarray:
    """Bootstrap distribution of the relative change in mean time per iteration."""
    rng = np.random.default_rng(0)
    b = rng.choice(base, size=(n_resamples, base.size)).mean(axis=1)
    n = rng.choice(new, size=(n_resamples, new.size)).mean(axis=1)
    return n / b - 1.0


def _short(name: str, width: int = 44) -> str:
    return name if len(name) <= width else "…" + name[-(width - 1) :]


def plot_change(plt, benches: list[Bench], theme: dict, threshold: float, path: Path) -> bool:
    """Ridgeline of bootstrapped % change per benchmark, with criterion's CI."""
    compared = [b for b in benches if b.change]
    if not compared:
        return False
    dists = [bootstrap_change(b.base.per_iter, b.new.per_iter) * 100 for b in compared]
    lows = [min(np.percentile(d, 0.5), b.change_mean["confidence_interval"]["lower_bound"] * 100) for d, b in zip(dists, compared)]
    highs = [max(np.percentile(d, 99.5), b.change_mean["confidence_interval"]["upper_bound"] * 100) for d, b in zip(dists, compared)]
    span = max(max(abs(x) for x in lows + highs), threshold * 100 * 2, 1.0) * 1.12
    grid = np.linspace(-span, span, 600)

    row_h = 0.42
    fig, ax = plt.subplots(figsize=(9, 1.2 + row_h * len(compared) * 1.5))
    ax.grid(axis="x", visible=True)
    ax.axvspan(-threshold * 100, threshold * 100, color=theme["band"], alpha=0.7, zorder=0, lw=0)
    ax.axvline(0, color=theme["muted"], lw=1, zorder=1)
    for i, (b, d) in enumerate(zip(compared, dists)):
        y = len(compared) - 1 - i
        density = kde(d, grid)
        density = density / density.max() * 0.82
        # Draw the ridge only where it has mass; the flat tails would otherwise
        # paint a full-width line in the verdict colour.
        keep = density > 0.004
        lo_i, hi_i = np.argmax(keep), len(keep) - np.argmax(keep[::-1])
        xs, ys = grid[lo_i:hi_i], density[lo_i:hi_i]
        colour = theme[b.verdict]
        ax.fill_between(xs, y, y + ys, color=colour, alpha=0.3, lw=0, zorder=2)
        ax.plot(xs, y + ys, color=colour, lw=1.6, zorder=3)
        ci = b.change_mean["confidence_interval"]
        est = b.change_mean["point_estimate"] * 100
        ax.plot([ci["lower_bound"] * 100, ci["upper_bound"] * 100], [y, y], color=colour, lw=3.5, solid_capstyle="round", zorder=4)
        ax.plot([est], [y], "o", ms=7, color=colour, mec=theme["ink"] if b.verdict == "unchanged" else colour, mew=0.8, zorder=5)
        # Label beside the interval on the side with more room.
        right = est <= span * 0.45
        edge = (max(ci["upper_bound"] * 100, xs[-1]) + span * 0.03) if right else (min(ci["lower_bound"] * 100, xs[0]) - span * 0.03)
        label = f"{est:+.1f}%" + (f"  {VERDICT_LABEL[b.verdict]}" if b.verdict in ("slower", "faster") else "")
        if not right:
            label = (f"{VERDICT_LABEL[b.verdict]}  " if b.verdict in ("slower", "faster") else "") + f"{est:+.1f}%"
        ax.annotate(
            label,
            (edge, y),
            ha="left" if right else "right",
            va="center",
            fontsize=8.5,
            color=theme["ink"] if b.verdict in ("slower", "faster") else theme["ink2"],
            fontweight="bold" if b.verdict in ("slower", "faster") else "normal",
            zorder=6,
        )
    ax.set_yticks(range(len(compared)), [_short(b.id) for b in reversed(compared)])
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="y", visible=False)
    ax.set_xlim(-span, span)
    ax.set_ylim(-0.45, len(compared) - 0.1)
    ax.set_xlabel("change in mean time per iteration vs base (%)  ·  ← faster   slower →")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:+.0f}%" if v else "0"))
    ax.set_title(
        f"Change distributions (bootstrap) · bar = criterion 95% CI · shaded = ±{threshold * 100:g}% noise band",
        fontsize=9,
        fontweight="normal",
        color=theme["ink2"],
    )
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return True


def _grid(plt, n: int, ncols: int, panel_w: float = 4.0, panel_h: float = 2.1):
    ncols = min(ncols, n)
    nrows = math.ceil(n / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(panel_w * ncols, panel_h * nrows + 0.5), squeeze=False)
    for ax in axes.flat[n:]:
        ax.set_visible(False)
    return fig, list(axes.flat[:n])


def _legend(fig, theme: dict, has_base: bool) -> None:
    from matplotlib.lines import Line2D

    handles = [Line2D([], [], color=theme["pr"], lw=2.2, label="this PR")]
    if has_base:
        handles.insert(0, Line2D([], [], color=theme["base"], lw=2.2, ls="--", label="base"))
    fig.legend(handles=handles, loc="upper right", ncol=2, fontsize=9, bbox_to_anchor=(0.995, 0.995))


def plot_distributions(plt, benches: list[Bench], theme: dict, path: Path, limit: int) -> bool:
    """Small multiples: KDE of time per iteration, base vs PR, with rug and medians."""
    shown = _prioritise([b for b in benches if b.new], limit)
    if not shown:
        return False
    fig, axes = _grid(plt, len(shown), ncols=3 if len(shown) > 4 else 2)
    for ax, b in zip(axes, shown):
        runs = [("base", b.base), ("pr", b.new)] if b.base else [("pr", b.new)]
        everything = np.concatenate([r.per_iter for _, r in runs])
        unit, scale = time_unit(np.median(everything))
        lo, hi = np.percentile(everything, [0.5, 99.5])
        pad = (hi - lo) * 0.25 or abs(hi) * 0.05
        grid = np.linspace(lo - pad, hi + pad, 400)
        peak = 0.0
        for name, run in runs:
            colour = theme[name]
            density = kde(run.per_iter, grid)
            peak = max(peak, density.max())
            ax.fill_between(grid / scale, density, color=colour, alpha=0.18 if name == "pr" else 0.12, lw=0)
            ax.plot(grid / scale, density, color=colour, lw=1.8, ls="--" if name == "base" else "-")
            ax.axvline(np.median(run.per_iter) / scale, color=colour, lw=1, ls=":")
        for name, run in runs:
            offset = -0.06 if name == "pr" else -0.12
            ax.plot(run.per_iter / scale, np.full(run.per_iter.size, offset * peak), "|", color=theme[name], ms=5, mew=0.8, alpha=0.7)
        ax.set_ylim(-0.17 * peak, peak * 1.12)
        ax.set_yticks([])
        ax.grid(axis="y", visible=False)
        ax.set_xlabel(f"time per iteration ({unit})")
        title = _short(b.id, 40)
        if b.change:
            title += f"   {b.change_mean['point_estimate'] * 100:+.1f}%"
        ax.set_title(title, color=theme[b.verdict] if b.verdict in ("slower", "faster") else theme["ink"])
    _legend(fig, theme, any(b.base for b in shown))
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return True


def plot_samples(plt, benches: list[Bench], theme: dict, path: Path, limit: int) -> bool:
    """Time per iteration in sample order: shows drift, warm-up and noisy neighbours."""
    shown = _prioritise([b for b in benches if b.new], limit)
    if not shown:
        return False
    fig, axes = _grid(plt, len(shown), ncols=3 if len(shown) > 4 else 2, panel_h=1.9)
    for ax, b in zip(axes, shown):
        unit, scale = time_unit(np.median(b.new.per_iter))
        if b.new.tukey:
            _, mild_lo, mild_hi, _ = b.new.tukey
            ax.axhspan(mild_lo / scale, mild_hi / scale, color=theme["band"], lw=0, zorder=0)
        for name, run in (("base", b.base), ("pr", b.new)):
            if run is None:
                continue
            ax.plot(np.arange(run.per_iter.size), run.per_iter / scale, "o", ms=3.2, color=theme[name], alpha=0.75 if name == "pr" else 0.55, mew=0)
        ax.set_ylabel(unit)
        ax.set_xlabel("sample #")
        ax.set_title(_short(b.id, 44))
    _legend(fig, theme, any(b.base for b in shown))
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return True


def _numeric(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def scaling_series(benches: list[Bench]) -> dict[str, list[Bench]]:
    """Group/function pairs benchmarked at two or more numeric parameter values."""
    series: dict[str, list[Bench]] = {}
    for b in benches:
        if b.new and _numeric(b.value) is not None:
            series.setdefault(f"{b.group}/{b.function}" if b.function else b.group, []).append(b)
    return {k: sorted(v, key=lambda b: _numeric(b.value)) for k, v in series.items() if len(v) >= 2}


def plot_scaling(plt, benches: list[Bench], theme: dict, path: Path) -> bool:
    """Log-log time vs input size per parameterised function, base vs PR."""
    series = scaling_series(benches)
    if not series:
        return False
    fig, axes = _grid(plt, len(series), ncols=2, panel_w=4.2, panel_h=2.6)
    for ax, (name, members) in zip(axes, series.items()):
        xs = np.array([_numeric(b.value) for b in members])
        for side, attr in (("base", "base"), ("pr", "new")):
            runs = [getattr(b, attr) for b in members]
            if not all(runs):
                continue
            est = np.array([r.typical["point_estimate"] for r in runs])
            lo = np.array([r.typical["confidence_interval"]["lower_bound"] for r in runs])
            hi = np.array([r.typical["confidence_interval"]["upper_bound"] for r in runs])
            ax.fill_between(xs, lo, hi, color=theme[side], alpha=0.18, lw=0)
            ax.plot(xs, est, "o-" if side == "pr" else "o--", color=theme[side], lw=2, ms=5)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: fmt_time(v)))
        ax.set_xlabel("parameter")
        ax.set_title(_short(name, 44))
    _legend(fig, theme, any(b.base for m in series.values() for b in m))
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return True


def _prioritise(benches: list[Bench], limit: int) -> list[Bench]:
    """Keep the ``limit`` most interesting benchmarks, in their original order."""
    if len(benches) <= limit:
        return benches
    rank = {"slower": 0, "faster": 1, "new": 2, "unchanged": 3}

    def key(b: Bench):
        size = abs(b.change_mean["point_estimate"]) if b.change else 0.0
        return (rank.get(b.verdict, 4), -size)

    keep = set(id(b) for b in sorted(benches, key=key)[:limit])
    return [b for b in benches if id(b) in keep]


PLOTS = ("change", "distributions", "samples", "scaling")


def render_plots(benches: list[Bench], out: Path, threshold: float, limit: int) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    made = []
    for mode, theme in THEMES.items():
        _style(plt, theme)
        drawn = {
            "change": plot_change(plt, benches, theme, threshold, out / f"change-{mode}.png"),
            "distributions": plot_distributions(plt, benches, theme, out / f"distributions-{mode}.png", limit),
            "samples": plot_samples(plt, benches, theme, out / f"samples-{mode}.png", limit),
            "scaling": plot_scaling(plt, benches, theme, out / f"scaling-{mode}.png"),
        }
        made = [name for name in PLOTS if drawn[name]]
    return made


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def picture(base_url: str, name: str, alt: str) -> str:
    """An image that follows the reader's GitHub light/dark theme."""
    return (
        "<picture>"
        f'<source media="(prefers-color-scheme: dark)" srcset="{base_url}/{name}-dark.png">'
        f'<img alt="{alt}" src="{base_url}/{name}-light.png">'
        "</picture>"
    )


def markdown(
    benches: list[Bench],
    threshold: float,
    title: str,
    image_base: str | None,
    plots: list[str],
    footer: str,
    limit: int,
) -> str:
    counts = {v: sum(b.verdict == v for b in benches) for v in VERDICT_ICON}
    lines = [f"## ⏱️ {title}", ""]
    if not benches:
        return "\n".join(lines + ["No criterion results were found.", "", footer])

    tally = " · ".join(f"{VERDICT_ICON[v]} **{counts[v]}** {VERDICT_LABEL[v]}" for v in VERDICT_ICON if counts[v])
    lines += [tally, ""]
    if counts["slower"]:
        worst = max((b for b in benches if b.verdict == "slower"), key=lambda b: b.change_mean["point_estimate"])
        lines += [f"> [!WARNING]\n> Largest regression: `{worst.id}` is **{fmt_pct(worst.change_mean['point_estimate'])}** slower.", ""]
    elif counts["faster"]:
        best = min((b for b in benches if b.verdict == "faster"), key=lambda b: b.change_mean["point_estimate"])
        lines += [f"> [!TIP]\n> Biggest win: `{best.id}` is **{fmt_pct(best.change_mean['point_estimate'])}** faster.", ""]

    if image_base and "change" in plots:
        lines += [picture(image_base, "change", "Bootstrapped change distribution per benchmark"), ""]

    has_tp = any(b.throughput for b in benches)
    header = "| | Benchmark | Base | This PR | Change (95% CI) |" + (" Throughput |" if has_tp else "")
    lines += [header, "|:-:|:--|--:|--:|:--|" + ("--:|" if has_tp else "")]
    for b in benches:
        base = fmt_time(b.base.typical["point_estimate"]) if b.base else "—"
        new = fmt_time(b.new.typical["point_estimate"]) if b.new else "—"
        if b.change:
            m = b.change_mean
            ci = m["confidence_interval"]
            change = f"{fmt_pct(m['point_estimate'])} <sub>[{fmt_pct(ci['lower_bound'])}, {fmt_pct(ci['upper_bound'])}]</sub>"
            if b.verdict in ("slower", "faster"):
                change = f"**{change}**"
        else:
            change = VERDICT_LABEL[b.verdict]
        row = f"| {VERDICT_ICON[b.verdict]} | `{b.id}` | {base} | {new} | {change} |"
        if has_tp:
            row += f" {fmt_throughput(b.throughput, b.new.typical['point_estimate']) if b.new else ''} |"
        lines.append(row)
    lines.append("")

    if image_base and "distributions" in plots:
        n_shown = min(limit, sum(1 for b in benches if b.new))
        note = "" if n_shown == sum(1 for b in benches if b.new) else f" (the {limit} largest changes)"
        lines += [
            f"### Distributions{note}",
            "",
            "Kernel density of time per iteration for each sample, base dashed and this PR solid, "
            "with every sample as a rug tick and the medians dotted. Shifted peaks are real changes; "
            "a wider or two-humped PR curve means the code has become less predictable even where the mean holds.",
            "",
            picture(image_base, "distributions", "Per-benchmark time distributions, base vs PR"),
            "",
        ]
    if image_base and "scaling" in plots:
        lines += [
            "<details><summary><b>📈 Scaling with input size</b></summary>",
            "",
            "Typical time against the benchmark parameter (log-log), with criterion's 95% interval shaded. "
            "A steeper PR line is a complexity change, not a constant-factor one.",
            "",
            picture(image_base, "scaling", "Time vs benchmark parameter"),
            "",
            "</details>",
            "",
        ]
    if image_base and "samples" in plots:
        lines += [
            "<details><summary><b>🔬 Raw samples (drift and noise check)</b></summary>",
            "",
            "Time per iteration in the order criterion took the samples; the band is the PR run's Tukey "
            "inner fences. A trend or step within one run means the runner was throttled or shared, and "
            "that run's numbers deserve less trust than the CI suggests.",
            "",
            picture(image_base, "samples", "Time per iteration by sample index"),
            "",
            "</details>",
            "",
        ]

    lines += [
        "<details><summary><b>ℹ️ How to read this</b></summary>",
        "",
        f"- *Change* is criterion's bootstrapped estimate of the relative change in mean time per iteration, "
        f"with its 95% confidence interval. A benchmark is only marked 🔴/🟢 when the **whole** interval lies beyond "
        f"±{threshold * 100:g}%, the noise threshold for shared CI runners (`threshold` input).",
        "- *Base* and *This PR* are criterion's typical time: the regression slope for linearly sampled benchmarks, the mean otherwise.",
        "- Base and PR are measured back to back on the same runner, so the comparison is fair even though absolute "
        "numbers vary between runners.",
        "- The full criterion HTML report (violin plots, regressions, t-tests) is attached to the workflow run as an artifact.",
        "",
        "</details>",
        "",
        footer,
    ]
    return "\n".join(lines)


def summary(benches: list[Bench]) -> dict:
    return {
        "counts": {v: sum(b.verdict == v for b in benches) for v in VERDICT_ICON},
        "benchmarks": [
            {
                "id": b.id,
                "verdict": b.verdict,
                "base_ns": b.base.typical["point_estimate"] if b.base else None,
                "new_ns": b.new.typical["point_estimate"] if b.new else None,
                "change": b.change_mean["point_estimate"] if b.change else None,
            }
            for b in benches
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("plots", "markdown"))
    parser.add_argument("--criterion-dir", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=5.0, help="noise threshold in percent")
    parser.add_argument("--base-name", default="base")
    parser.add_argument("--new-name", default="new")
    parser.add_argument("--limit", type=int, default=12, help="most panels per small-multiples figure")
    parser.add_argument("--out", type=Path, help="plots: output directory")
    parser.add_argument("--image-base", default="", help="markdown: URL prefix the PNGs are served from")
    parser.add_argument("--plots", default="", help="markdown: comma-separated plots that were rendered")
    parser.add_argument("--title", default="Criterion benchmarks")
    parser.add_argument("--footer", default="")
    args = parser.parse_args(argv)

    threshold = args.threshold / 100
    benches = load(args.criterion_dir, args.base_name, args.new_name)
    classify(benches, threshold)

    if args.command == "plots":
        args.out.mkdir(parents=True, exist_ok=True)
        made = render_plots(benches, args.out, threshold, args.limit) if benches else []
        result = summary(benches) | {"plots": made}
        (args.out / "summary.json").write_text(json.dumps(result, indent=2))
        print(",".join(made))
    else:
        plots = [p for p in args.plots.split(",") if p]
        sys.stdout.write(markdown(benches, threshold, args.title, args.image_base or None, plots, args.footer, args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
