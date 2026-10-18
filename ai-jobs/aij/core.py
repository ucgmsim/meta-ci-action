"""Plumbing shared by every job: config, shell and gh calls, the CI gate, Claude, publishing.

A job runs as one `aij run <job>` process in a GitHub Actions job (see
.github/workflows/ai-jobs.yml), on the repo that called the workflow. Its steps share
what they learn through `Run.data`.
"""

import datetime as dt
import json
import os
import re
import string
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import ghapp

HERE = Path(__file__).resolve().parent  # prompts/ and defaults.toml
DATA = Path(os.environ.get("AIJ_DATA", Path(tempfile.gettempdir()) / "aij"))
WORK = DATA / "work"  # the bot's clones: the repo, and meta-ci-action for the CI jobs
LOG_DIR = DATA / "logs"  # the job log, Claude's JSON results, dry-run patches and PR text; uploaded as an artifact
STATE_DIR = DATA / "state"  # rotation state, kept between runs in the Actions cache
LOCKFILES = {"uv.lock", "Cargo.lock"}
LABELS = {
    "ai-generated": ("5319e7", "Opened by the scheduled Claude jobs"),
    "ai-chore": ("c5def5", "Mechanical bot PR (lockfile, lint)"),
    "ai-fix": ("fbca04", "Bot-written fix that needs review"),
    "ai-tests": ("bfd4f2", "Bot-written tests (coverage or mutation) that need review"),
    "ai-ci": ("f9d0c4", "Bot-written CI change that needs review"),
    "ai-ok": ("0e8a16", "Vetted: the bot may attempt a fix"),
    "ai-stuck": ("d93f0b", "The bot tried and gave up; see its comment"),
    "ai-suggested": ("d4c5f9", "Filed by the weekly issue sweep"),
    "ai-serious": ("b60205", "The issue sweep found a serious bug; needs a human"),
}
CLAUDE_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(uv run:*)", "Bash(cargo test:*)", "Bash(cargo build:*)", "Bash(cargo clippy:*)",
    "Bash(git diff:*)", "Bash(git status:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(.ai/ci-checks:*)", "Bash(cargo fmt:*)",
]  # fmt: skip
# Appended to every headless run's system prompt: lessons from runs that went wrong.
CLAUDE_RULES = """\
You are running headless and unattended; the session ends the moment you stop.
- Run every command in the foreground and wait for it, with a long timeout (up to
  600000 ms). Never start background tasks or say you're "waiting" for one: nothing
  resumes you. `.ai/ci-checks` can take several minutes; `.ai/ci-checks --command <name>`
  runs one check quickly.
- Put any scratch files under .ai/ only; everything else in the working tree may be
  committed.
- You cannot edit git-hook config such as lefthook.yml; the script handles it. Don't
  try, and don't spend turns probing what you're allowed to do.
- Finish by writing the output files your task asks for, even if some work is left:
  describe what's left in the PR body rather than stopping without writing them.
"""
CLAUDE_TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"
CLAUDE_FOOTER = "\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"
STDOUT_TAIL = 4000  # chars of each command's output echoed to the Actions log; the job log gets it all
# Kept from Claude's environment (CLAUDE_CODE_OAUTH_TOKEN is Claude's own login, so it stays).
SECRET_ENV = {"AIJ_GH_APP_ID", "AIJ_GH_APP_KEY", "GH_TOKEN", "GITHUB_TOKEN"}
class Skip(Exception):
    """A job decided not to open a PR. The message says why."""


class Done(Exception):
    """A job finished early with nothing to do. The message is its status."""


@dataclass
class Repo:
    slug: str
    cfg: dict

    @property
    def name(self) -> str:
        return self.slug.split("/")[1]

    @property
    def path(self) -> Path:
        return WORK / self.name

    @property
    def default(self) -> str:
        return self.cfg["default"]

    @property
    def is_meta(self) -> bool:
        return self.slug == self.cfg["meta_repo"]


def merge(base: dict, over: dict) -> dict:
    """base with over's keys on top; tables such as budgets are merged key by key."""
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def default_branch(slug: str) -> str:
    return sh(["gh", "api", f"repos/{slug}", "--jq", ".default_branch"], cwd=HERE).stdout.strip()


def load_repo(slug: str, config: str = "") -> Repo:
    """The repo's settings: defaults.toml, then the caller's `config` TOML on top."""
    cfg = merge(tomllib.loads((HERE / "defaults.toml").read_text()), tomllib.loads(config))
    cfg["default"] = cfg["default"] or default_branch(slug)
    return Repo(slug, cfg)


def meta_repo(repo: Repo) -> Repo:
    """meta-ci-action, as a second clone for the CI jobs (only its default branch matters)."""
    slug = repo.cfg["meta_repo"]
    return repo if repo.is_meta else Repo(slug, repo.cfg | {"default": default_branch(slug)})


@dataclass
class Run:
    """One job on one repo. Its steps share what they learn through `data`."""

    job: str
    repo: Repo
    live: bool
    data: dict = field(default_factory=dict)


class Log:
    """The job log; commands and their output go here, and (trimmed) to stdout for the Actions log."""

    fh = None

    @classmethod
    def open(cls, name: str) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        cls.fh = open(LOG_DIR / f"{name}.log", "a")  # noqa: SIM115 - held for the whole job

    @classmethod
    def write(cls, text: str, echo: bool = True) -> None:
        text = text.rstrip()
        if cls.fh:
            cls.fh.write(text + "\n")
            cls.fh.flush()
        if echo and text:
            print(text if len(text) <= STDOUT_TAIL else "(trimmed; full output in the job log artifact)\n" + text[-STDOUT_TAIL:],
                  flush=True)  # fmt: skip


def env() -> dict:
    return dict(os.environ)


def sh(cmd, cwd: Path, check: bool = True, timeout: int = 1800, **kw) -> subprocess.CompletedProcess:
    shell = isinstance(cmd, str)
    Log.write(f"$ {cmd if shell else ' '.join(map(str, cmd))}")
    p = subprocess.run(
        cmd, cwd=cwd, shell=shell, text=True, capture_output=True, check=False,
        timeout=timeout, env=kw.pop("env", env()), **kw,
    )  # fmt: skip
    Log.write(p.stdout + p.stderr)
    if check and p.returncode:
        raise subprocess.CalledProcessError(p.returncode, cmd, p.stdout, p.stderr)
    return p


def gh(repo: Repo, *args: str, check: bool = True) -> str:
    return sh(["gh", *args, "-R", repo.slug], cwd=HERE, check=check).stdout


def gh_json(repo: Repo, *args: str) -> list | dict:
    return json.loads(gh(repo, *args) or "[]")


def recently(date: str | None, days: int) -> bool:
    return bool(date) and dt.date.fromisoformat(date) > dt.date.today() - dt.timedelta(days=days)


def load_state(repo: Repo) -> dict:
    path = STATE_DIR / f"{repo.name}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(repo: Repo, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{repo.name}.json").write_text(json.dumps(state, indent=1, sort_keys=True))


def is_test_path(path: str) -> bool:
    p = Path(path)
    return "tests" in p.parts or "test" in p.parts or p.name.startswith("test_") or p.name == "conftest.py"


def ranges(lines: list[int]) -> str:
    """[1, 2, 3, 7] -> '1-3, 7'."""
    out, start, prev = [], None, None
    for n in sorted(lines):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(out)


def today() -> str:
    return dt.date.today().isoformat()


# --- repo plumbing ---------------------------------------------------------


def fetch(repo: Repo) -> None:
    if not repo.path.exists():
        WORK.mkdir(parents=True, exist_ok=True)
        sh(["gh", "repo", "clone", repo.slug, str(repo.path), "--", "--quiet"], cwd=WORK)
    sh(["git", "fetch", "--prune", "--tags", "--force", "--quiet", "origin"], cwd=repo.path)


def prepare(repo: Repo, branch: str, base: str | None = None) -> None:
    """Reset the bot's clone to a fresh branch off the default branch (or `base`, e.g. a tag)."""
    fetch(repo)
    sh(["git", "switch", "--quiet", "-f", "-C", branch, base or f"origin/{repo.default}"], cwd=repo.path)
    # Keep build caches and in-place compiled extensions (uv sync won't rebuild a deleted
    # PyO3 .so for an already-installed editable package); drop everything else.
    sh(["git", "clean", "-fdxq", "-e", ".venv", "-e", "target", "-e", "*.so", "-e", "*.pyd"], cwd=repo.path)
    exclude = repo.path / ".git/info/exclude"
    if "mutants/" not in exclude.read_text():
        exclude.write_text(exclude.read_text() + "\n.ai/\nmutants/\n")
    (repo.path / ".ai").mkdir(exist_ok=True)
    write_ci_checks(repo)


def sync(repo: Repo) -> None:
    try:
        sh(["uv", "sync", "--quiet", *repo.cfg["sync"]], cwd=repo.path)
    except subprocess.CalledProcessError as e:
        raise Skip(f"uv sync failed: {e.stderr.strip().splitlines()[-1:]}") from e


def tests_pass(repo: Repo) -> bool:
    minutes = repo.cfg["test_timeout_minutes"]
    return sh(repo.cfg["test"], cwd=repo.path, check=False, timeout=minutes * 60).returncode == 0


def write_ci_checks(repo: Repo) -> None:
    """Give the clone .ai/ci-checks: meta-ci-action's lefthook gate, the same checks ci.yml runs.

    LEFTHOOK_CONFIG points lefthook at our own config, so this works whether or
    not the repo has adopted the shared lefthook.yml yet.
    """
    ai = repo.path / ".ai"
    (ai / "lefthook-ci.yml").write_text(
        "remotes:\n"
        "  - git_url: https://github.com/ucgmsim/meta-ci-action\n"
        f"    ref: {repo.cfg['lefthook_ref']}\n"
        "    refetch: true\n"
        "    configs:\n"
        "      - lefthook/ci.yml\n"
    )
    script = ai / "ci-checks"
    script.write_text(
        "#!/bin/sh\n"
        "# Runs every check CI runs (meta-ci-action's lefthook/ci.yml pre-push gate).\n"
        "# Extra args go to lefthook, e.g. `.ai/ci-checks --command ty`.\n"
        'cd "$(git rev-parse --show-toplevel)" || exit 1\n'
        "export LEFTHOOK_CONFIG=.ai/lefthook-ci.yml\n"
        "L=\"uvx --from lefthook==2.1.14 lefthook\"\n"
        "# lefthook only fetches remote configs on install; without it, run has no commands.\n"
        '$L install >/dev/null 2>&1 || { echo "ci-checks: lefthook install failed" >&2; exit 2; }\n'
        "# No --force: it would run commands whose glob matches nothing (the Rust checks in a repo\n"
        "# with no crate), where CI's detect job skips them.\n"
        'exec $L run pre-push --all-files --no-tty --no-auto-install --colors off "$@"\n'
    )
    script.chmod(0o755)


def failing_checks(repo: Repo) -> set[str]:
    """Names of the CI checks that fail right now. Tests are left to tests_pass()."""
    return run_gate(repo)[0]


def run_gate(repo: Repo) -> tuple[set[str], str]:
    """Run .ai/ci-checks (tests excluded); return the failing check names and the output."""
    p = sh([".ai/ci-checks"], cwd=repo.path, check=False, timeout=repo.cfg["ci_checks_timeout_minutes"] * 60,
           env=env() | {"LEFTHOOK_EXCLUDE": "tests"})  # fmt: skip
    out = p.stdout + p.stderr
    # Summary lines are "✓/✗ <command> (time)" with --colors off; the header's 🥊 is mid-line.
    # A run that reports nothing ran no checks (e.g. the remote config didn't load): never
    # treat that as passing, or every change would look safe.
    if not re.search(r"^(?:✓|✗|✔️|🥊)\s+\S+ \(", out, re.MULTILINE):
        raise Skip(f"the CI gate ran no checks (exit {p.returncode}); see the log")
    return set(re.findall(r"^(?:✗|🥊)\s+(\S+) \(", out, re.MULTILINE)), out


def gate_report(failing: set[str], output: str, limit: int = 12000) -> str:
    """The gate result for a prompt: which checks fail, then the output (tail-trimmed)."""
    if not failing:
        return "All of the shared CI's checks pass (tests aside; the script runs those)."
    text = output if len(output) <= limit else "(trimmed)\n" + output[-limit:]
    return f"Failing: {', '.join(sorted(failing))}\n\n```\n{text}\n```"


def no_new_failures(repo: Repo, before: set[str]) -> None:
    """Tests passing isn't enough: CI also runs lint and type checks. Skip if the change broke one."""
    if new := failing_checks(repo) - before:
        raise Skip(f"the change makes these checks fail: {sorted(new)}")


def dirty(repo: Repo) -> bool:
    return bool(sh(["git", "status", "--porcelain"], cwd=repo.path).stdout.strip())


def dirty_path(repo: Repo, path: str) -> bool:
    return bool(sh(["git", "status", "--porcelain", "--", path], cwd=repo.path).stdout.strip())


def changed_paths(repo: Repo) -> list[str]:
    sh(["git", "add", "-A"], cwd=repo.path)
    return sh(["git", "diff", "--cached", "--name-only"], cwd=repo.path).stdout.split()


def commits_ahead(repo: Repo) -> int:
    return int(sh(["git", "rev-list", "--count", f"origin/{repo.default}..HEAD"], cwd=repo.path).stdout.strip() or 0)


def open_bot_prs(repo: Repo, label: str) -> list[dict]:
    return gh_json(
        repo, "pr", "list", "--author", ghapp.author(), "--label", label, "--state", "open",
        "--json", "number,headRefName,updatedAt,url,labels",
    )  # fmt: skip


def check_cap(repo: Repo, prefix: str | None = None) -> None:
    """Skip when the repo is at its review-PR cap, or this job already has a PR open."""
    prs = [pr for pr in open_bot_prs(repo, "ai-generated") if all(lbl["name"] != "ai-chore" for lbl in pr["labels"])]
    if len(prs) >= repo.cfg["max_open_review_prs"]:
        raise Skip(f"at the cap of {repo.cfg['max_open_review_prs']} open review PRs")
    if prefix and (mine := [pr["url"] for pr in prs if pr["headRefName"].startswith(prefix)]):
        raise Skip(f"this job's last PR is still open: {mine[0]}")


def ensure_labels(repo: Repo) -> None:
    existing = {lbl["name"] for lbl in gh_json(repo, "label", "list", "--limit", "200", "--json", "name")}
    for name, (colour, desc) in LABELS.items():
        if name not in existing:
            gh(repo, "label", "create", name, "--color", colour, "--description", desc)


def number(url: str) -> str:
    """The PR or issue number at the end of a github.com URL."""
    return url.rstrip("/").rsplit("/", 1)[-1]


def request_review(repo: Repo, pr: str) -> str:
    """Ask the repo's reviewer to review a PR the bot opened; a failure is noted, not fatal."""
    if not (who := repo.cfg.get("reviewer")):
        return ""
    p = sh(["gh", "api", "-X", "POST", f"repos/{repo.slug}/pulls/{pr}/requested_reviewers",
            "-f", f"reviewers[]={who}"], cwd=HERE, check=False)  # fmt: skip
    return f", review requested from @{who}" if p.returncode == 0 else f" (couldn't request a review from @{who})"


def create_issue(repo: Repo, title: str, body: str, labels: list[str], live: bool) -> str:
    """File an issue (or, in a dry run, write it down for `promote`). ai-serious ones go to the reviewer."""
    if not live:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / f"{repo.name}-issues.md", "a") as f:
            f.write(f"# {title}\n\nLabels: {', '.join(labels)}\n\n{body}\n\n---\n\n")
        return f"(dry run) {title}"
    url = gh(repo, "issue", "create", "--title", title, "--body", body, "--label", ",".join(labels)).strip()
    if "ai-serious" in labels and (who := repo.cfg.get("reviewer")):
        # REST, not `gh issue edit`: that goes through GraphQL and trips over the retired Projects (classic) API.
        p = sh(["gh", "api", "-X", "POST", f"repos/{repo.slug}/issues/{number(url)}/assignees",
                "-f", f"assignees[]={who}"], cwd=HERE, check=False)  # fmt: skip
        url += f" (assigned to @{who})" if p.returncode == 0 else f" (couldn't assign @{who})"
    return url


def file_bug_report(repo: Repo, job: str, live: bool) -> str:
    """Turn .ai/bug-report.md, written by a test-writing job, into an ai-suggested issue."""
    report = repo.path / ".ai/bug-report.md"
    if not report.exists():
        return ""
    title, _, body = report.read_text().strip().partition("\n")
    body = f"{body.strip()}\n\n_Found by the scheduled `{job}` job while writing tests; needs triage._"
    return " | bug filed: " + create_issue(repo, title.lstrip("# ").strip()[:120], body, ["ai-suggested"], live)


def pr_text(repo: Repo) -> tuple[str, str]:
    """Read Claude's .ai/pr-title and .ai/pr-body.md, or skip with its .ai/skip-reason."""
    ai = repo.path / ".ai"
    if reason := skip_reason(repo):
        raise Skip(reason)
    if not (ai / "pr-title").exists() or not (ai / "pr-body.md").exists():
        raise Skip("Claude finished without writing the PR title and body")
    title = (ai / "pr-title").read_text().strip().splitlines()[0][:100]
    # The footer is added by cost_line(); drop any copy Claude wrote itself.
    body = (ai / "pr-body.md").read_text().replace(CLAUDE_FOOTER.strip(), "").strip()
    return title, body


def skip_reason(repo: Repo) -> str:
    """Claude's .ai/skip-reason, if it wrote a non-empty one (an empty file means "don't skip")."""
    path = repo.path / ".ai/skip-reason"
    return path.read_text().strip() if path.exists() else ""


def apply_deletions(repo: Repo, prefix: str) -> list[str]:
    """Delete the files Claude listed in .ai/delete (it has no rm).

    Untracked files (Claude's own scratch files) may go from anywhere in the repo;
    tracked files only from under `prefix`.
    """
    listing = repo.path / ".ai/delete"
    if not listing.exists():
        return []
    deleted = []
    root = repo.path.resolve()
    for line in listing.read_text().split():
        target = (repo.path / line).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            Log.write(f"ignoring delete request for {line!r}: outside the repo or missing")
            continue
        tracked = sh(["git", "ls-files", "--error-unmatch", str(target)], cwd=repo.path, check=False).returncode == 0
        if tracked and not target.is_relative_to((repo.path / prefix).resolve()):
            raise Skip(f"Claude asked to delete tracked file {line!r}, which is outside {prefix}")
        target.unlink()
        deleted.append(line)
    listing.unlink()
    Log.write(f"deleted at Claude's request: {deleted}")
    return deleted


def cost_line(runs: list[dict]) -> str:
    cost = sum(r.get("total_cost_usd", 0) for r in runs)
    turns = sum(r.get("num_turns") or 0 for r in runs)
    runs_note = f" over {len(runs)} runs" if len(runs) > 1 else ""
    return f"\n\n_Cost ${cost:.2f}, {turns} turns{runs_note}._" + CLAUDE_FOOTER


# .ai/ci-checks installs lefthook's hooks in the clone; the script runs the gate itself, so skip them.
NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]


def publish(
    repo: Repo, job: str, branch: str, title: str, body: str, labels: list[str],
    live: bool, allowed=lambda path: True, count_lockfiles: bool = False, trailer: str = "",
    draft: bool = False,
) -> str | None:  # fmt: skip
    """Check the working tree against the limits, then commit, push and open or update the PR.

    Commits the job already made on the branch (script autofixes) go along too; the size
    and path limits apply to the uncommitted part, which is what Claude wrote.
    """
    sh(["git", "add", "-A"], cwd=repo.path)
    numstat = sh(["git", "diff", "--cached", "--numstat"], cwd=repo.path).stdout.splitlines()
    base = f"origin/{repo.default}"
    ahead = commits_ahead(repo)
    if not numstat and not ahead:
        return None
    size, paths = 0, []
    for line in numstat:
        added, removed, path = line.split("\t", 2)
        paths.append(path)
        if count_lockfiles or Path(path).name not in LOCKFILES:
            size += int(added if added != "-" else 0) + int(removed if removed != "-" else 0)
    if bad := [p for p in paths if not allowed(p)]:
        raise Skip(f"touches paths this job may not change: {bad}")
    limit = repo.cfg.get("diff_limits", {}).get(job, repo.cfg["max_diff_lines"])
    if size > limit:
        raise Skip(f"diff is {size} lines, over the {limit} limit")

    if not live:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        patch = LOG_DIR / f"{repo.name}-{job}.patch"
        # Against the base, so the patch includes the job's own commits as well as Claude's changes.
        patch.write_text(sh(["git", "diff", "--cached", base], cwd=repo.path).stdout)
        (LOG_DIR / f"{repo.name}-{job}.pr.md").write_text(f"# {title}\n\n{body}\n")
        extra = f" + {ahead} script commit(s)" if ahead else ""
        return f"dry run{' (draft)' if draft else ''}: {size} lines over {len(paths)} files{extra}, patch at {patch}"

    if numstat:
        commit(repo, f"{title}\n\n{body}" + (f"\n\n{trailer}" if trailer else ""))
    try:
        sh(["git", *NO_HOOKS, "push", "--quiet", "--force-with-lease", "-u", "origin", branch], cwd=repo.path)
    except subprocess.CalledProcessError as e:
        hint = " (changing .github/workflows needs the GitHub App's Workflows: write)" if "workflow" in e.stderr else ""
        raise Skip(f"push rejected{hint}: {e.stderr.strip().splitlines()[-1:]}") from e
    existing = gh_json(repo, "pr", "list", "--head", branch, "--state", "open", "--json", "number,url")
    if existing:
        # REST, not `gh pr edit`: that goes through GraphQL and trips over the retired Projects (classic) API.
        sh(["gh", "api", "-X", "PATCH", f"repos/{repo.slug}/pulls/{existing[0]['number']}",
            "-f", f"title={title}", "-F", "body=@-"], cwd=HERE, input=body)  # fmt: skip
        return f"updated {existing[0]['url']}"
    url = gh(repo, "pr", "create", "--base", repo.default, "--head", branch, "--title", title,
             "--body", body, "--label", ",".join(labels), *(["--draft"] if draft else [])).strip()  # fmt: skip
    return f"opened {'draft ' if draft else ''}{url}{request_review(repo, number(url))}"


def commit(repo: Repo, message: str) -> bool:
    """Commit everything in the working tree; False if there was nothing to commit."""
    sh(["git", "add", "-A"], cwd=repo.path)
    if not sh(["git", "diff", "--cached", "--name-only"], cwd=repo.path).stdout.strip():
        return False
    sh(["git", *NO_HOOKS, "commit", "--quiet", "-F", "-"], cwd=repo.path, input=message)
    return True


def run_claude(repo: Repo, prompt: str, name: str, job: str, add_dirs: tuple[Path, ...] = ()) -> dict:
    """Run Claude headless in the clone. It gets edit tools and test runners, no gh or push.

    Returns the cost summary of the run (the full JSON result goes to logs/<date>/<name>.claude.json).
    """
    # No GitHub App key, no tokens and an empty gh config mean neither gh nor git's credential
    # helper can authenticate from inside Claude's session; pushing is the script's job.
    minutes = repo.cfg.get("timeouts", {}).get(job, repo.cfg["timeout_minutes"])
    with tempfile.TemporaryDirectory() as ghdir:
        e = {k: v for k, v in env().items() if k not in SECRET_ENV and not k.startswith("ACTIONS_")}
        e |= {"GH_CONFIG_DIR": ghdir, "GIT_TERMINAL_PROMPT": "0"}
        cmd = [
            "claude", "-p", "--model", repo.cfg.get("models", {}).get(job, repo.cfg["model"]),
            "--max-budget-usd", str(repo.cfg["budgets"][job]),
            "--output-format", "json", "--permission-mode", "dontAsk",
            "--strict-mcp-config", "--no-session-persistence", "--append-system-prompt", CLAUDE_RULES,
            "--allowedTools", ",".join(CLAUDE_TOOLS),
        ]  # fmt: skip
        if add_dirs:
            cmd += ["--add-dir", *map(str, add_dirs)]
        Log.write(f"$ claude ({name}), prompt {len(prompt)} chars")
        try:
            p = subprocess.run(
                cmd, cwd=repo.path, input=prompt, text=True, capture_output=True, check=False,
                env=e, timeout=minutes * 60,
            )  # fmt: skip
        except subprocess.TimeoutExpired as exc:
            raise Skip(f"Claude timed out after {minutes} min") from exc
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / f"{name}.claude.json").write_text(p.stdout)
    Log.write(p.stderr)
    try:
        result = json.loads(p.stdout)
    except json.JSONDecodeError as exc:
        raise Skip(f"Claude exited {p.returncode} without a JSON result") from exc
    Log.write(f"claude: {result.get('subtype')} turns={result.get('num_turns')} cost=${result.get('total_cost_usd', 0):.2f}")
    if result.get("is_error"):
        raise Skip(f"Claude run ended with {result.get('subtype')}")
    return {k: result.get(k) for k in ("subtype", "num_turns", "total_cost_usd")}


def prompt(name: str, **values) -> str:
    return string.Template((HERE / "prompts" / f"{name}.md").read_text()).safe_substitute(**values)
