"""The jobs, each a list of steps `aij run <job>` runs in order.

A step is `fn(run)`. It raises Skip (the job gives up, with a reason) or Done (nothing
to do, with a status) to stop the run early, and returns a status string from the last
step. Anything a later step needs goes in `run.data`.
"""

import collections
import json
import re
from collections.abc import Callable

from . import core
from .core import (
    CLAUDE_TRAILER,
    Done,
    Log,
    Repo,
    Run,
    Skip,
    check_cap,
    cost_line,
    create_issue,
    failing_checks,
    gh,
    gh_json,
    is_test_path,
    load_state,
    no_new_failures,
    pr_text,
    prepare,
    prompt,
    recently,
    run_claude,
    save_state,
    sh,
    sync,
    tests_pass,
    today,
)  # fmt: skip

Step = Callable[[Run], str | None]
JOBS: dict[str, dict[str, Step]] = {}


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
