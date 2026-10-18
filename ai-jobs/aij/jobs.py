"""The jobs, each a list of steps `aij run <job>` runs in order.

A step is `fn(run)`. It raises Skip (the job gives up, with a reason) or Done (nothing
to do, with a status) to stop the run early, and returns a status string from the last
step. Anything a later step needs goes in `run.data`.
"""

import collections
import json
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import core
from .core import (
    CLAUDE_TRAILER,
    LOCKFILES,
    Done,
    Log,
    Repo,
    Run,
    Skip,
    apply_deletions,
    changed_paths,
    check_cap,
    commit,
    commits_ahead,
    cost_line,
    create_issue,
    dirty,
    dirty_path,
    failing_checks,
    fetch,
    file_bug_report,
    gate_report,
    gh,
    gh_json,
    is_test_path,
    load_state,
    no_new_failures,
    pr_text,
    prepare,
    prompt,
    ranges,
    recently,
    run_claude,
    run_gate,
    save_state,
    sh,
    skip_reason,
    sync,
    tests_pass,
    today,
)  # fmt: skip

Step = Callable[[Run], str | None]
JOBS: dict[str, dict[str, Step]] = {}
META_JOBS = {"ci-promote"}  # jobs that run on meta-ci-action, and only there


def step(job: str, name: str):
    def register(fn: Step) -> Step:
        JOBS.setdefault(job, {})[name] = fn
        return fn

    return register


def before(r: Run) -> set[str]:
    """The checks that already failed on the default branch, recorded by the baseline step."""
    return set(r.data["before"])


def baseline(r: Run) -> None:
    r.data["before"] = sorted(failing_checks(r.repo))
    Log.write(f"checks failing before the change: {r.data['before'] or 'none'}")


def claude_runs(r: Run) -> list[dict]:
    return r.data.setdefault("runs", [])


# --- chores: no Claude -----------------------------------------------------


@step("lockfile", "prepare")
def lockfile_prepare(r: Run) -> None:
    prepare(r.repo, "ai/lockfile")
    sync(r.repo)


# Upgraded linters and type checkers (ruff, ty) can flag existing code, so compare against before.
step("lockfile", "baseline")(baseline)


@step("lockfile", "upgrade")
def lockfile_upgrade(r: Run) -> None:
    """Weekly: refresh uv.lock and Cargo.lock in one PR, replacing lockfile-only Dependabot PRs."""
    repo = r.repo
    changes = []
    if (repo.path / "uv.lock").exists():
        out = sh(["uv", "lock", "--upgrade"], cwd=repo.path).stderr
        changes += re.findall(r"^Updated (.+)$", out, re.MULTILINE)
    if (repo.path / "Cargo.lock").exists():
        out = sh(["cargo", "update"], cwd=repo.path).stderr
        changes += [m.strip() for m in re.findall(r"^\s*(?:Updating|Upgrading) (.+ -> .+)$", out, re.MULTILINE)]
    if not dirty(repo):
        raise Done("lockfiles already current")
    sync(repo)
    if not tests_pass(repo):
        # Tell a bad upgrade apart from a suite that already fails here (e.g. a missing system library).
        sh(["git", "stash", "--quiet"], cwd=repo.path)
        sync(repo)
        if not tests_pass(repo):
            raise Skip("tests already fail on the default branch in this environment; see the log")
        raise Skip("tests pass on the default branch but fail with the upgraded lockfile; see the log")
    r.data["listed"] = "\n".join(f"- {c}" for c in changes) or "- (see diff)"
    commit(repo, f"Refresh lockfiles\n\n{r.data['listed']}")


@step("lockfile", "autofix")
def lockfile_autofix(r: Run) -> None:
    """Upgraded ruff/ty/clippy can flag existing code: first try the tools' own autofixes."""
    r.data["notes"] = []
    new = failing_checks(r.repo) - before(r)
    if new and (fixed := autofix(r.repo, new)):
        r.data["notes"].append(fixed)
        new = failing_checks(r.repo) - before(r)
    r.data["new"] = sorted(new)


@step("lockfile", "repair")
def lockfile_repair(r: Run) -> str | None:
    """Then a Claude repair round for what the autofixes couldn't fix (e.g. ty)."""
    if not r.data["new"]:
        Log.write("no checks newly failing; nothing to repair")
        return None
    failing, output = run_gate(r.repo)
    text = prompt("repair", context="a lockfile refresh", repo=r.repo.slug, gate=gate_report(failing - before(r), output))
    claude_runs(r).append(run_claude(r.repo, text, f"{r.repo.name}-lockfile-repair", "lockfile"))
    r.data["new"] = sorted(failing_checks(r.repo) - before(r))
    return None


@step("lockfile", "verify")
def lockfile_verify(r: Run) -> None:
    if r.data["new"]:
        raise Skip(f"the upgrade makes these checks fail and the repair couldn't fix them: {r.data['new']}")
    if (r.data["notes"] or claude_runs(r)) and not tests_pass(r.repo):
        raise Skip("the tests fail after fixing the checks the upgrade broke")


@step("lockfile", "publish")
def lockfile_publish(r: Run) -> str:
    repo, runs, notes = r.repo, claude_runs(r), r.data["notes"]
    body = f"Weekly lockfile refresh. The full test suite passed locally with these versions.\n\n{r.data['listed']}"
    if notes:
        body += "\n\n## Fixes for the upgraded tools\n\n" + "\n".join(f"- {n}" for n in notes)
    if runs and (repo.path / ".ai/pr-body.md").exists():
        body += "\n\n## Fixed by Claude\n\n" + pr_text(repo)[1] + cost_line(runs)
    # Only lockfiles and mechanical autofixes: a chore. Claude-written code: needs a review.
    labels = ["ai-generated", "ai-fix" if runs else "ai-chore"]
    result = core.publish(
        repo, "lockfile", "ai/lockfile", "Refresh lockfiles", body, labels, r.live,
        trailer=CLAUDE_TRAILER if runs else "",
    )  # fmt: skip
    if result and r.live:
        url = result.split()[-1]
        for pr in gh_json(repo, "pr", "list", "--author", "app/dependabot", "--state", "open", "--json", "number,files"):
            if {f["path"] for f in pr["files"]} <= LOCKFILES:
                gh(repo, "pr", "close", str(pr["number"]), "--comment", f"Superseded by {url}, which refreshes the whole lockfile.")
    return result or "no changes"


@step("lint", "prepare")
def lint_prepare(r: Run) -> None:
    prepare(r.repo, "ai/lint")
    sync(r.repo)


step("lint", "baseline")(baseline)


@step("lint", "fix")
def lint_fix(r: Run) -> None:
    """Weekly: apply ruff's safe fixes (and formatting where CI checks it)."""
    repo = r.repo
    # Lint with the ruff CI uses: the locked one, or the latest for repos whose CI runs an unpinned ruff action.
    locked = sh(["uv", "run", "--no-sync", "ruff", "--version"], cwd=repo.path, check=False).returncode == 0
    ruff = ["uv", "run", "--no-sync", "ruff"] if locked and not repo.cfg.get("ruff_latest") else ["uvx", "ruff@latest"]
    r.data["ruff"] = ruff
    sh([*ruff, "check", "--fix", "--quiet", "."], cwd=repo.path, check=False)
    if repo.cfg["ruff_format"]:
        sh([*ruff, "format", "--quiet", "."], cwd=repo.path, check=False)
    if not dirty(repo):
        raise Done("nothing to fix")


@step("lint", "verify")
def lint_verify(r: Run) -> None:
    if not tests_pass(r.repo):
        raise Skip("tests fail after ruff's fixes; see the log")
    no_new_failures(r.repo, before(r))


@step("lint", "publish")
def lint_publish(r: Run) -> str:
    repo = r.repo
    left = sh([*r.data["ruff"], "check", "--output-format", "concise", "."], cwd=repo.path, check=False).stdout
    remaining = len([ln for ln in left.splitlines() if re.match(r"^\S+:\d+:\d+:", ln)])
    body = "Applies ruff's safe autofixes" + (" and formatting" if repo.cfg["ruff_format"] else "") + ". Tests pass locally."
    if remaining:
        body += f"\n\n{remaining} findings remain that need a human (not auto-fixable)."
    return core.publish(
        repo, "lint", "ai/lint", "Apply ruff safe fixes", body, ["ai-generated", "ai-chore"], r.live,
        allowed=lambda p: not p.startswith(".github/"),
    ) or "no changes"  # fmt: skip


# --- ai-ok fixes -----------------------------------------------------------


def mark_stuck(repo: Repo, issue: int, reason: str, live: bool) -> None:
    Log.write(f"stuck #{issue}: {reason}")
    if live:
        gh(repo, "issue", "comment", str(issue), "--body", f"The scheduled fix job gave up on this issue:\n\n> {reason}\n\nRemove `ai-stuck` and re-add `ai-ok` to retry.")
        gh(repo, "issue", "edit", str(issue), "--remove-label", "ai-ok", "--add-label", "ai-stuck")


def open_closing_prs(repo: Repo, issue: int) -> list[str]:
    """URLs of open PRs, by anyone, that will close the issue (a closing keyword or a Development link)."""
    owner, name = repo.slug.split("/")
    query = (
        "query($owner: String!, $name: String!, $n: Int!) { repository(owner: $owner, name: $name) {"
        " issue(number: $n) { closedByPullRequestsReferences(first: 20) { nodes { state url } } } } }"
    )
    out = sh(["gh", "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}",
              "-F", f"n={issue}"], cwd=core.HERE).stdout  # fmt: skip
    prs = json.loads(out)["data"]["repository"]["issue"]["closedByPullRequestsReferences"]["nodes"]
    return [pr["url"] for pr in prs if pr["state"] == "OPEN"]


def next_ai_ok_issue(repo: Repo) -> int | None:
    taken = {pr["headRefName"] for pr in core.open_bot_prs(repo, "ai-fix")}
    issues = gh_json(repo, "issue", "list", "--label", "ai-ok", "--state", "open", "--limit", "50",
                     "--search", "sort:created-asc", "--json", "number,labels")  # fmt: skip
    for issue in issues:
        n = issue["number"]
        if f"ai/issue-{n}" in taken or any(lbl["name"] == "ai-stuck" for lbl in issue["labels"]):
            continue
        if closing := open_closing_prs(repo, n):
            Log.write(f"#{n}: already has an open PR that closes it ({', '.join(closing)}), skipping")
            continue
        events = json.loads(sh(["gh", "api", f"repos/{repo.slug}/issues/{n}/events", "--paginate"], cwd=core.HERE).stdout)
        labellers = [ev["actor"]["login"] for ev in events if ev["event"] == "labeled" and ev["label"]["name"] == "ai-ok"]
        if labellers and labellers[-1] in repo.cfg["maintainers"]:
            return n
        Log.write(f"#{n}: ai-ok not applied by a maintainer ({labellers[-1:]}), ignoring")
    return None


@step("ai-ok", "pick")
def ai_ok_pick(r: Run) -> None:
    """Daily: attempt the oldest maintainer-labelled `ai-ok` issue, if under the PR cap."""
    check_cap(r.repo)
    # --issue (the ISSUE param): try one issue, bypassing the ai-ok label check.
    issue = r.data.get("force_issue") or next_ai_ok_issue(r.repo)
    if issue is None:
        raise Done("no eligible ai-ok issues")
    r.data["issue"] = issue
    Log.write(f"working on #{issue}")


@step("ai-ok", "prepare")
def ai_ok_prepare(r: Run) -> None:
    prepare(r.repo, f"ai/issue-{r.data['issue']}")
    sync(r.repo)


step("ai-ok", "baseline")(baseline)


@step("ai-ok", "claude")
def ai_ok_claude(r: Run) -> None:
    repo, issue = r.repo, r.data["issue"]
    r.data["stuck_on_skip"] = True  # from here on, giving up marks the issue ai-stuck (see cli.finish)
    # JSON, not the text view: `gh issue view` text output trips over the retired Projects (classic) API.
    data = gh_json(repo, "issue", "view", str(issue), "--json", "title,author,body,comments")
    issue_text = f"# {data['title']}\n(opened by {data['author']['login']})\n\n{data['body']}" + "".join(
        f"\n\n## Comment by {c['author']['login']}\n\n{c['body']}" for c in data["comments"]
    )
    text = prompt("ai-ok", issue=issue, repo=repo.slug, test=repo.cfg["test"],
                  max_diff_lines=repo.cfg["max_diff_lines"], issue_text=issue_text)  # fmt: skip
    claude_runs(r).append(run_claude(repo, text, f"{repo.name}-ai-ok-{issue}", "ai-ok"))


@step("ai-ok", "verify")
def ai_ok_verify(r: Run) -> None:
    title, body = pr_text(r.repo)
    if not tests_pass(r.repo):
        raise Skip("the test suite fails with the proposed fix")
    no_new_failures(r.repo, before(r))
    if f"#{r.data['issue']}" not in body:
        body = f"Closes #{r.data['issue']}\n\n{body}"
    r.data["title"], r.data["body"] = title, body


@step("ai-ok", "publish")
def ai_ok_publish(r: Run) -> str:
    issue = r.data["issue"]
    out = core.publish(
        r.repo, f"ai-ok-{issue}", f"ai/issue-{issue}", r.data["title"], r.data["body"] + cost_line(claude_runs(r)),
        ["ai-generated", "ai-fix"], r.live, allowed=lambda p: not p.startswith(".github/"), trailer=CLAUDE_TRAILER,
    )  # fmt: skip
    if out is None:
        raise Skip("Claude made no changes")
    return out


# --- test improvement ------------------------------------------------------


def measure_coverage(repo: Repo) -> dict[str, dict]:
    """Line coverage per source file: {path: {"pct": float, "missing": [lines]}}."""
    report = repo.path / ".ai/coverage.json"
    report.unlink(missing_ok=True)
    cmd = [
        "uv", "run", "--no-sync", "--with", "pytest-cov", "pytest", *repo.cfg["pytest_paths"],
        "-q", "-p", "no:cacheprovider", f"--cov={repo.cfg['cov_package']}", f"--cov-report=json:{report}",
    ]  # fmt: skip
    if repo.cfg.get("cov_ignore_omit"):
        # The repo's own coverage config omits files we want targeted; measure with a blank one.
        rc = repo.path / ".ai/empty.coveragerc"
        rc.write_text("[run]\n")
        cmd.append(f"--cov-config={rc}")
    sh(cmd, cwd=repo.path, check=False, timeout=repo.cfg["test_timeout_minutes"] * 60)
    if not report.exists():
        raise Skip("the coverage run produced no report; see the log")
    files = json.loads(report.read_text())["files"]
    return {p: {"pct": f["summary"]["percent_covered"], "missing": f["missing_lines"]} for p, f in files.items()}


@step("coverage", "cap")
def coverage_cap(r: Run) -> None:
    check_cap(r.repo, "ai/coverage")


@step("coverage", "prepare")
def coverage_prepare(r: Run) -> None:
    prepare(r.repo, "ai/coverage")
    sync(r.repo)


step("coverage", "baseline")(baseline)


@step("coverage", "target")
def coverage_target(r: Run) -> None:
    """Weekly: tests for the least-covered file not targeted recently. The PR may only touch tests."""
    repo = r.repo
    if not repo.cfg["cov_package"]:
        raise Done("no cov_package configured")
    cov = measure_coverage(repo)
    state = load_state(repo)
    done = state.setdefault("coverage", {})
    candidates = sorted(
        (v["pct"], -len(v["missing"]), p)
        for p, v in cov.items()
        if len(v["missing"]) >= repo.cfg["min_missing_lines"]
        and not is_test_path(p)
        and not recently(done.get(p), repo.cfg["rotate_days"])
    )
    if not candidates:
        raise Done("no coverage targets left in the rotation")
    pct, _, target = candidates[0]
    done[target] = today()
    save_state(repo, state)
    r.data |= {"target": target, "pct": pct, "missing": cov[target]["missing"]}
    Log.write(f"target {target} at {pct:.1f}%")


@step("coverage", "claude")
def coverage_claude(r: Run) -> None:
    repo = r.repo
    text = prompt("coverage", repo=repo.slug, target=r.data["target"], percent=f"{r.data['pct']:.0f}",
                  missing=ranges(r.data["missing"]), test=repo.cfg["test"])  # fmt: skip
    claude_runs(r).append(run_claude(repo, text, f"{repo.name}-coverage", "coverage"))


@step("coverage", "verify")
def coverage_verify(r: Run) -> None:
    repo, target, pct = r.repo, r.data["target"], r.data["pct"]
    bug = r.data["bug"] = file_bug_report(repo, "coverage", r.live)
    try:
        title, body = pr_text(repo)
    except Skip as e:
        raise Skip(f"{e}{bug}") from e
    if not tests_pass(repo):
        raise Skip(f"the new tests fail{bug}")
    no_new_failures(repo, before(r))
    new = measure_coverage(repo).get(target, {}).get("pct", pct)
    if new <= pct:
        raise Skip(f"coverage of {target} did not rise from {pct:.1f}%{bug}")
    r.data["title"] = title
    r.data["body"] = body + f"\n\nCoverage of `{target}`: {pct:.1f}% → {new:.1f}%." + cost_line(claude_runs(r))


@step("coverage", "publish")
def coverage_publish(r: Run) -> str:
    out = core.publish(
        r.repo, "coverage", "ai/coverage", r.data["title"], r.data["body"], ["ai-generated", "ai-tests"], r.live,
        allowed=is_test_path, trailer=CLAUDE_TRAILER,
    )  # fmt: skip
    return (out or "Claude made no test changes") + r.data["bug"]


def mutation_candidates(repo: Repo) -> list[tuple[str, str]]:
    """(path, kind) pairs for source files big enough to be worth mutating."""
    found = []
    for d in repo.cfg.get("mut_python", []):
        found += [(str(p.relative_to(repo.path)), "py") for p in (repo.path / d).rglob("*.py")]
    if rust := repo.cfg.get("mut_rust"):
        found += [(str(p.relative_to(repo.path)), "rs") for p in (repo.path / rust["dir"]).rglob("*.rs")]
    return [
        (p, k) for p, k in found
        if not is_test_path(p) and Path(p).name not in {"__init__.py", "tests.rs"}
        and len((repo.path / p).read_text().splitlines()) >= repo.cfg["mut_min_lines"]
    ]  # fmt: skip


def mutate(repo: Repo, target: str, kind: str) -> list[str]:
    """Run mutation testing on one file; return one description (with diff for Python) per survivor."""
    timeout = repo.cfg["mutation_timeout_minutes"] * 60
    try:
        if kind == "rs":
            out = repo.path / ".ai/mut"
            shutil.rmtree(out, ignore_errors=True)
            sh(["cargo", "mutants", "-p", repo.cfg["mut_rust"]["package"], "--file", target,
                "--no-shuffle", "-j", "2", "--timeout", "300", "-o", str(out)],
               cwd=repo.path, check=False, timeout=timeout)  # fmt: skip
            if not (out / "mutants.out/outcomes.json").exists():
                raise Skip("cargo mutants did not complete; see the log")
            missed = out / "mutants.out/missed.txt"
            return missed.read_text().splitlines() if missed.exists() else []

        pyproject = repo.path / "pyproject.toml"
        original = pyproject.read_text()
        if "[tool.mutmut]" in original:
            raise Skip("the repo has its own [tool.mutmut] config, which this job doesn't merge with yet")
        top = target.split("/")[0]
        config = (
            f'\n[tool.mutmut]\nsource_paths = ["{top}"]\nonly_mutate = ["{target}"]\n'
            f"pytest_add_cli_args_test_selection = {json.dumps(repo.cfg['pytest_paths'])}\n"
            f'also_copy = ["{top}"]\n'
        )
        shutil.rmtree(repo.path / "mutants", ignore_errors=True)
        mutmut = ["uv", "run", "--no-sync", "--with", "mutmut", "mutmut"]
        try:
            pyproject.write_text(original + config)
            sh([*mutmut, "run"], cwd=repo.path, check=False, timeout=timeout)
            results = sh([*mutmut, "results", "--all", "true"], cwd=repo.path, check=False).stdout
            statuses = collections.Counter(re.findall(r"^\s*\S+: (.+)$", results, re.MULTILINE))
            if not statuses.get("killed") and not statuses.get("survived"):
                raise Skip(f"mutmut checked no mutants ({dict(statuses)}); the baseline test run probably failed, see the log")
            names = re.findall(r"^\s*(\S+): survived$", results, re.MULTILINE)
            return [sh([*mutmut, "show", n], cwd=repo.path, check=False).stdout.strip() for n in names]
        finally:
            pyproject.write_text(original)
    except subprocess.TimeoutExpired as e:
        raise Skip(f"mutation testing of {target} timed out") from e


@step("mutants", "cap")
def mutants_cap(r: Run) -> None:
    check_cap(r.repo, "ai/mutants")


@step("mutants", "prepare")
def mutants_prepare(r: Run) -> None:
    prepare(r.repo, "ai/mutants")
    sync(r.repo)


step("mutants", "baseline")(baseline)


@step("mutants", "target")
def mutants_target(r: Run) -> None:
    """Weekly: mutate one file, then have Claude write tests that catch the survivors."""
    repo = r.repo
    state = load_state(repo)
    done = state.setdefault("mutants", {})
    candidates = sorted(
        (done.get(p, ""), p, k) for p, k in mutation_candidates(repo)
        if not recently(done.get(p), repo.cfg["rotate_days"])
    )  # fmt: skip
    if not candidates:
        raise Done("no mutation targets left in the rotation")
    _, target, kind = candidates[0]
    done[target] = today()
    save_state(repo, state)
    r.data |= {"target": target, "kind": kind}
    Log.write(f"mutating {target}")
    survivors = r.data["survivors"] = mutate(repo, target, kind)
    if not survivors:
        raise Done(f"no surviving mutants in {target}")


@step("mutants", "claude")
def mutants_claude(r: Run) -> None:
    repo, before_mutants, kind = r.repo, r.data["survivors"], r.data["kind"]
    shown = before_mutants[:25] if kind == "py" else before_mutants[:40]
    survivors = ("\n\n" if kind == "py" else "\n").join(shown)
    if len(before_mutants) > len(shown):
        survivors += f"\n\n({len(before_mutants) - len(shown)} more not shown.)"
    text = prompt("mutants", repo=repo.slug, target=r.data["target"], survivors=survivors, test=repo.cfg["test"])
    claude_runs(r).append(run_claude(repo, text, f"{repo.name}-mutants", "mutants"))


@step("mutants", "verify")
def mutants_verify(r: Run) -> None:
    repo, target, kind, before_mutants = r.repo, r.data["target"], r.data["kind"], r.data["survivors"]
    bug = r.data["bug"] = file_bug_report(repo, "mutants", r.live)
    try:
        title, body = pr_text(repo)
    except Skip as e:
        raise Skip(f"{e}{bug}") from e
    if not tests_pass(repo):
        raise Skip(f"the new tests fail{bug}")
    no_new_failures(repo, before(r))
    after = mutate(repo, target, kind)
    if len(after) >= len(before_mutants):
        raise Skip(f"the new tests killed no mutants ({len(before_mutants)} survive){bug}")
    body += f"\n\nSurviving mutants in `{target}`: {len(before_mutants)} → {len(after)}."
    if kind == "rs":
        body += " Rust tests live in the file's `#[cfg(test)]` module, so check that only test code changed."
    r.data["title"], r.data["body"] = title, body + cost_line(claude_runs(r))


@step("mutants", "publish")
def mutants_publish(r: Run) -> str:
    target = r.data["target"]
    # Rust unit tests sit in the source file itself, so that one file is allowed too.
    allowed = (lambda p: is_test_path(p) or p == target) if r.data["kind"] == "rs" else is_test_path
    out = core.publish(
        r.repo, "mutants", "ai/mutants", r.data["title"], r.data["body"], ["ai-generated", "ai-tests"], r.live,
        allowed=allowed, trailer=CLAUDE_TRAILER,
    )  # fmt: skip
    return (out or "Claude made no test changes") + r.data["bug"]


# --- issue sweep -----------------------------------------------------------


def sweep_focus(repo: Repo, done: dict, budget_lines: int = 2500) -> list[str]:
    """Source files not swept recently, most-changed first, up to about budget_lines."""
    files = sh(["git", "ls-files", "*.py", "*.rs"], cwd=repo.path).stdout.split()
    churn = collections.Counter(
        sh(["git", "log", "--since=90.days", "--name-only", "--format="], cwd=repo.path).stdout.split()
    )
    candidates = sorted(
        (done.get(p, ""), -churn[p], p) for p in files
        if not is_test_path(p) and not p.startswith(("docs/", "scripts/one_offs/"))
        and not recently(done.get(p), repo.cfg["rotate_days"])
    )  # fmt: skip
    focus, total = [], 0
    for _, _, p in candidates:
        n = len((repo.path / p).read_text(errors="replace").splitlines())
        if n < 20:
            continue
        focus.append(p)
        total += n
        if total >= budget_lines:
            break
    return focus


def normalise(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


@step("issue-sweep", "cap")
def sweep_cap(r: Run) -> None:
    """Weekly: review a slice of the repo and file vetted issues; low-risk ones get ai-ok."""
    open_suggestions = gh_json(r.repo, "issue", "list", "--label", "ai-suggested", "--state", "open", "--limit", "100", "--json", "number")
    if len(open_suggestions) >= r.repo.cfg["max_open_suggestions"]:
        raise Done(f"{len(open_suggestions)} ai-suggested issues still open; skipping until some are handled")


@step("issue-sweep", "prepare")
def sweep_prepare(r: Run) -> None:
    prepare(r.repo, "ai/sweep")
    sync(r.repo)


@step("issue-sweep", "focus")
def sweep_select(r: Run) -> None:
    focus = sweep_focus(r.repo, load_state(r.repo).get("sweep", {}))
    if not focus:
        raise Done("every file was swept recently")
    r.data["focus"] = focus
    r.data["existing"] = gh_json(r.repo, "issue", "list", "--state", "all", "--limit", "300", "--json", "number,title,state")
    Log.write("focus:\n" + "\n".join(focus))


@step("issue-sweep", "claude")
def sweep_claude(r: Run) -> None:
    repo, focus, existing = r.repo, r.data["focus"], r.data["existing"]
    existing_text = "\n".join(f"- #{i['number']} [{i['state'].lower()}] {i['title']}" for i in existing) or "(none)"
    text = prompt("issue-sweep", repo=repo.slug, focus="\n".join(f"- {p}" for p in focus),
                  existing=existing_text, max_issues=repo.cfg["max_issues_per_sweep"])  # fmt: skip
    claude_runs(r).append(run_claude(repo, text, f"{repo.name}-issue-sweep", "issue-sweep"))
    state = load_state(repo)
    done = state.setdefault("sweep", {})
    for p in focus:
        done[p] = today()
    save_state(repo, state)


@step("issue-sweep", "file")
def sweep_file(r: Run) -> str:
    repo, cfg, focus = r.repo, r.repo.cfg, r.data["focus"]
    path = repo.path / ".ai/issues.json"
    if not path.exists():
        raise Skip("Claude wrote no .ai/issues.json")
    try:
        proposed = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise Skip(f".ai/issues.json is not valid JSON: {e}") from e
    if not proposed:
        return f"nothing found in {len(focus)} files"

    seen = {normalise(i["title"]) for i in r.data["existing"]}
    filed, serious, ok = [], 0, 0
    for item in proposed[: cfg["max_issues_per_sweep"]]:
        title, body = str(item.get("title", "")).strip(), str(item.get("body", "")).strip()
        files = [f for f in item.get("files", []) if (repo.path / f).exists()]
        if not title or not body or not files or normalise(title) in seen:
            Log.write(f"dropped proposal: {title!r} (missing fields, unknown files or duplicate)")
            continue
        seen.add(normalise(title))
        severity = item.get("severity", "low")
        labels = ["ai-suggested"]
        footer = f"\n\n_Filed by the weekly issue sweep. Kind: {item.get('kind', '?')}, severity: {severity}._"
        if severity in ("critical", "high"):
            labels.append("ai-serious")
            serious += 1
            if cfg["serious_mentions"]:
                footer += "\n\ncc " + " ".join(f"@{u}" for u in cfg["serious_mentions"])
        elif item.get("ai_ok") and cfg["auto_ai_ok"]:
            labels.append("ai-ok")
            ok += 1
        filed.append(create_issue(repo, title[:120], body + footer, labels, r.live))
    return f"filed {len(filed)} issues ({serious} serious, {ok} ai-ok) from {len(focus)} files: " + "; ".join(filed)


# --- CI conformance --------------------------------------------------------


def ci_status(repo: Repo) -> str:
    """Latest run of each workflow on the default branch, with log tails for failures."""
    runs = gh_json(repo, "run", "list", "--branch", repo.default, "--limit", "40",
                   "--json", "workflowName,conclusion,status,databaseId,createdAt")  # fmt: skip
    latest = {}
    for run in runs:
        latest.setdefault(run["workflowName"], run)
    if not latest:
        return "(no recent runs on the default branch)"
    lines, failures = [], []
    for name, run in latest.items():
        lines.append(f"- {name}: {run['conclusion'] or run['status']} ({run['createdAt'][:10]})")
        if run["conclusion"] == "failure" and len(failures) < 3:
            log = gh(repo, "run", "view", str(run["databaseId"]), "--log-failed", check=False)
            failures.append(f"\nFailed log tail for {name}:\n```\n" + "\n".join(log.splitlines()[-40:])[-3000:] + "\n```")
    return "\n".join(lines + failures)


def actionlint(repo: Repo) -> None:
    workflows = [p for p in changed_paths(repo) if p.startswith(".github/workflows/") and (repo.path / p).exists()]
    if not workflows:
        return
    p = sh(["uvx", "--from", "actionlint-py", "actionlint", *workflows], cwd=repo.path, check=False)
    if p.returncode:
        raise Skip("actionlint rejects the changed workflows: " + " | ".join(p.stdout.splitlines()[:5]))


LEFTHOOK_REMOTE = ("ucgmsim/meta-ci-action", "lefthook/ci.yml")


# Commands meta-ci-action's lefthook/ci.yml defines, plus older names repos used for the
# same checks. A repo's own copy of these is dead code once the remote loads (the remote wins).
SHARED_HOOK_COMMANDS = {
    "pre-commit": {"ruff-fix", "ruff-format", "rustfmt", "ty", "ruff"},
    "pre-push": {"ruff", "ruff-format", "deptry", "npdlint", "ty", "pytest", "rustfmt", "clippy",
                 "cargo-test", "numpydoc"},
}  # fmt: skip
NEW_LEFTHOOK_HEADER = """\
lefthook.yml

Git hooks that run the same checks as CI, from ucgmsim/meta-ci-action's
lefthook/ci.yml (pinned to v2, like the CI workflow). Set up once per clone:

  uv sync --all-extras --dev
  uvx lefthook install

Turn shared checks off with `exclude_tags`; add repo-specific hooks under new names.
"""


def adopt_lefthook(repo: Repo) -> str | None:
    """Make lefthook.yml load the shared config at v2, dropping local copies of its commands.

    Done by the script because Claude Code won't edit git-hook config headlessly. Uses
    round-trip YAML so the repo's comments and extra hooks survive.
    """
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    path = repo.path / "lefthook.yml"
    existed = path.exists()
    data = (yaml.load(path) if existed else None) or CommentedMap()

    remotes = data.get("remotes") or CommentedSeq()
    ours = [r for r in remotes if LEFTHOOK_REMOTE[0] in str(r.get("git_url", ""))]
    removed = []
    if ours:
        entry = ours[0]
        entry["ref"] = "v2"
        if LEFTHOOK_REMOTE[1] not in (entry.get("configs") or []):
            entry.setdefault("configs", CommentedSeq()).append(LEFTHOOK_REMOTE[1])
    else:
        entry = CommentedMap(git_url="https://github.com/ucgmsim/meta-ci-action", ref="v2",
                             configs=CommentedSeq([LEFTHOOK_REMOTE[1]]))  # fmt: skip
        remotes.append(entry)
    if "remotes" not in data:
        data.insert(0, "remotes", remotes)

    for hook, names in SHARED_HOOK_COMMANDS.items():
        commands = (data.get(hook) or {}).get("commands")
        if not commands:
            continue
        for name in [n for n in commands if n in names]:
            del commands[name]
            removed.append(f"{hook}/{name}")
        if not commands:
            del data[hook]["commands"]
        if not any(k for k in data[hook] if k not in ("parallel", "piped")):
            del data[hook]

    # A fresh top level: the old file's header ("a local mirror of ci.yml ...") and comments
    # that sat above removed hooks no longer describe it. Kept hooks keep their own comments.
    rebuilt = CommentedMap()
    rebuilt["remotes"] = remotes
    for key, value in data.items():
        if key != "remotes":
            rebuilt[key] = value
    rebuilt.yaml_set_start_comment(NEW_LEFTHOOK_HEADER)
    data = rebuilt
    before_text = path.read_text() if existed else ""
    with path.open("w") as f:
        yaml.dump(data, f)
    if path.read_text() == before_text:
        return None
    check_lefthook(repo, True)
    what = "Add lefthook.yml loading" if not existed else "Switch lefthook.yml to"
    detail = f"\n\nDropped local copies of shared commands: {', '.join(removed)}." if removed else ""
    message = f"{what} meta-ci-action's shared hooks at v2{detail}"
    commit(repo, message)
    return message.replace("\n\n", " ")


def locked_ruff(repo: Repo) -> list[str]:
    """uvx invocation of the ruff version pinned in uv.lock (what CI's ruff job uses)."""
    lock = repo.path / "uv.lock"
    m = re.search(r'^name = "ruff"\nversion = "([^"]+)"', lock.read_text(), re.MULTILINE) if lock.exists() else None
    return ["uvx", f"ruff@{m[1] if m else 'latest'}"]


def autofix(repo: Repo, failing: set[str]) -> str | None:
    """Apply the fixes the failing checks' own tools can make, as one commit."""
    applied = []
    ruff = locked_ruff(repo)
    if "ruff" in failing:
        sh([*ruff, "check", "--fix", "--force-exclude", "."], cwd=repo.path, check=False)
        applied.append("ruff check --fix")
    if "ruff-format" in failing:
        sh([*ruff, "format", "--force-exclude", "."], cwd=repo.path, check=False)
        applied.append("ruff format")
    if "rustfmt" in failing:
        sh(["cargo", "fmt", "--all"], cwd=repo.path, check=False)
        applied.append("cargo fmt")
    if "clippy" in failing:
        sh(["cargo", "clippy", "--fix", "--allow-dirty", "--allow-staged", "--all-targets"],
           cwd=repo.path, check=False, timeout=3600)  # fmt: skip
        applied.append("cargo clippy --fix")
    if not applied:
        return None
    message = f"Apply {', '.join(applied)} for checks the shared CI runs"
    return message if commit(repo, message) else None


def check_lefthook(repo: Repo, allowed: bool) -> None:
    """Refuse a lefthook.yml change that's premature, broken, or doesn't load the shared config."""
    if "lefthook.yml" not in changed_paths(repo):
        return
    if not allowed:
        raise Skip("changed lefthook.yml although meta-ci-action's v2 has no shared config yet")
    text = (repo.path / "lefthook.yml").read_text() if (repo.path / "lefthook.yml").exists() else ""
    if not all(s in text for s in LEFTHOOK_REMOTE) or "ref: v2" not in text:
        raise Skip("the new lefthook.yml doesn't load meta-ci-action's lefthook/ci.yml at v2")
    p = sh(["uvx", "--from", "lefthook==2.1.14", "lefthook", "validate"], cwd=repo.path, check=False)
    if p.returncode:
        raise Skip("lefthook validate rejects lefthook.yml: " + " | ".join((p.stdout + p.stderr).splitlines()[:3]))


@step("ci-conformance", "cap")
def ci_cap(r: Run) -> None:
    """Weekly: one PR bringing a repo's CI in line with meta-ci-action."""
    check_cap(r.repo, "ai/ci")


@step("ci-conformance", "prepare")
def ci_prepare(r: Run) -> None:
    meta = core.meta_repo(r.repo)
    prepare(meta, "ai/readonly", base="v2")  # repos should pin to v2, so compare against what v2 offers
    prepare(r.repo, "ai/ci")
    sync(r.repo)


@step("ci-conformance", "baseline")
def ci_baseline(r: Run) -> None:
    r.data["before"] = sorted(run_gate(r.repo)[0])
    Log.write(f"checks failing before the change: {r.data['before'] or 'none'}")


@step("ci-conformance", "mechanical")
def ci_mechanical(r: Run) -> None:
    """The mechanical parts, done by the script as their own commits: Claude can't edit
    hook config headlessly, and formatters shouldn't cost a model run."""
    done = r.data["done"] = []
    hooks_allowed = r.data["hooks_allowed"] = (core.meta_repo(r.repo).path / LEFTHOOK_REMOTE[1]).exists()
    if hooks_allowed and (adopted := adopt_lefthook(r.repo)):
        done.append(adopted)
    if fixed := autofix(r.repo, before(r)):
        done.append(fixed)
    Log.write("applied by the script:\n" + ("\n".join(done) or "nothing"))


@step("ci-conformance", "claude")
def ci_claude(r: Run) -> None:
    repo, meta = r.repo, core.meta_repo(r.repo)
    failing, output = run_gate(repo)
    text = prompt(
        "ci-conformance", repo=repo.slug, meta_dir=str(meta.path), ci_status=ci_status(repo),
        baseline=(core.HERE / "prompts/ci-baseline.md").read_text(), gate=gate_report(failing, output),
        done="\n".join(f"- {d}" for d in r.data["done"]) or "- nothing",
    )  # fmt: skip
    claude_runs(r).append(run_claude(repo, text, f"{repo.name}-ci-conformance", "ci-conformance", add_dirs=(meta.path,)))
    apply_deletions(repo, ".github/workflows")


@step("ci-conformance", "repair")
def ci_repair(r: Run) -> None:
    """Give Claude the checks that still fail, up to ci_repair_attempts times, rather than giving up."""
    repo = r.repo
    for attempt in range(1, repo.cfg["ci_repair_attempts"] + 1):
        failing, output = run_gate(repo)
        if not failing:
            break
        repair = prompt("repair", context="a CI conformance change", repo=repo.slug, gate=gate_report(failing, output))
        claude_runs(r).append(run_claude(repo, repair, f"{repo.name}-ci-repair-{attempt}", "ci-conformance",
                                         add_dirs=(core.meta_repo(r.repo).path,)))  # fmt: skip
        apply_deletions(repo, ".github/workflows")
    # Claude's `uv run`/`uv lock` calls re-lock as a side effect; lockfile changes belong to the
    # lockfile job, so put them back and re-check against the committed lock.
    if restored := [f for f in LOCKFILES if (repo.path / f).exists() and dirty_path(repo, f)]:
        sh(["git", "checkout", "HEAD", "--", *restored], cwd=repo.path)
        Log.write(f"restored lockfiles Claude changed: {restored}")
        sync(repo)
    r.data["failing"] = sorted(run_gate(repo)[0])


@step("ci-conformance", "verify")
def ci_verify(r: Run) -> None:
    repo, failing = r.repo, set(r.data["failing"])
    reason = r.data["reason"] = skip_reason(repo)
    changed = changed_paths(repo)
    ahead = commits_ahead(repo)
    if not changed and not ahead:
        raise Done("already conformant and green" if "conformant" in reason.lower() or not failing else f"no changes: {reason}")
    actionlint(repo)
    check_lefthook(repo, r.data["hooks_allowed"])
    if new := failing - before(r):
        raise Skip(f"the change makes checks fail that passed before: {sorted(new)}")
    code_changed = any(not p.startswith(".github/") for p in changed) or ahead
    if code_changed and not tests_pass(repo):
        raise Skip("the test suite fails with the proposed change")


@step("ci-conformance", "publish")
def ci_publish(r: Run) -> str:
    repo, failing, reason, done = r.repo, r.data["failing"], r.data["reason"], r.data["done"]
    ai = repo.path / ".ai"
    if not reason and (ai / "pr-title").exists() and (ai / "pr-body.md").exists():
        title, body = pr_text(repo)
    else:
        # Claude skipped or wrote nothing, but the script's commits (hooks, autofixes) still stand.
        title = "Bring CI in line with meta-ci-action"
        body = "Automated CI conformance changes." + (f"\n\nClaude's note: {reason}" if reason else "")
    if done:
        body += "\n\n## Applied by the script\n\n" + "\n".join(f"- {d}" for d in done)
    if failing:
        # Opened as a draft rather than dropped: the fixes so far are worth keeping.
        body += ("\n\n## Still failing\n\nThese checks already failed on the default branch and the "
                 f"automated repair couldn't fix them: {', '.join(failing)}. Draft until they're fixed.")  # fmt: skip
    return core.publish(
        repo, "ci-conformance", "ai/ci", title, body + cost_line(claude_runs(r)), ["ai-generated", "ai-ci"], r.live,
        trailer=CLAUDE_TRAILER, draft=bool(failing),
    ) or "no changes"  # fmt: skip


# On meta-ci-action itself: move CI that several repos duplicate into it, backward compatibly.


@step("ci-promote", "cap")
def promote_cap(r: Run) -> None:
    check_cap(r.repo, "ai/ci")


@step("ci-promote", "prepare")
def promote_prepare(r: Run) -> None:
    prepare(r.repo, "ai/ci-promote")


@step("ci-promote", "consumers")
def promote_consumers(r: Run) -> None:
    """Copy each consuming repo's .github into .ai/consumers/<repo> for Claude to compare.

    The consumers are the repos the bot's GitHub App is installed on, which are the repos running these jobs.
    """
    consumers = r.repo.path / ".ai/consumers"
    listing = sh(["gh", "api", "installation/repositories", "--paginate", "--jq",
                  ".repositories[] | select(.archived | not) | [.full_name, .default_branch] | @tsv"], cwd=core.HERE)  # fmt: skip
    for line in listing.stdout.splitlines():
        slug, default = line.split("\t")
        other = Repo(slug, r.repo.cfg | {"default": default})
        if other.is_meta:
            continue
        fetch(other)
        (consumers / other.name).mkdir(parents=True, exist_ok=True)
        sh(f"git archive origin/{shlex.quote(other.default)} .github | tar -x -C {shlex.quote(str(consumers / other.name))}",
           cwd=other.path, check=False)  # fmt: skip


@step("ci-promote", "claude")
def promote_claude(r: Run) -> None:
    baseline_text = (core.HERE / "prompts/ci-baseline.md").read_text()
    text = prompt("ci-promote", consumers_dir=".ai/consumers", baseline=baseline_text)
    # Budgeted as ci-conformance, as in ai_jobs.py: it was that job's meta-repo branch.
    claude_runs(r).append(run_claude(r.repo, text, f"{r.repo.name}-ci-promote", "ci-conformance"))


@step("ci-promote", "publish")
def promote_publish(r: Run) -> str:
    title, body = pr_text(r.repo)
    apply_deletions(r.repo, ".github/workflows")
    actionlint(r.repo)
    return core.publish(
        r.repo, "ci-promote", "ai/ci-promote", title, body + cost_line(claude_runs(r)), ["ai-generated", "ai-ci"],
        r.live, trailer=CLAUDE_TRAILER,
    ) or "Claude made no changes"  # fmt: skip
