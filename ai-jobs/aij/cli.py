"""Entry points the ai-jobs workflow calls (.github/workflows/ai-jobs.yml).

    aij run <job> [--live true] [--issue N]   run a job on $GITHUB_REPOSITORY, configured by $AIJ_CONFIG
    aij gh-app token|identity                 a GitHub App token (bin/gh), or the bot's git name and email
"""

import argparse
import os
import sys
import traceback

from . import core, ghapp
from .core import Done, Log, Run, Skip
from .jobs import JOBS, mark_stuck


def truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on", "live"}


def run_steps(r: Run) -> str:
    """Run the job's steps in order; the status is the last step's, or why it stopped early."""
    if r.live:
        core.ensure_labels(r.repo)
    status = "done"
    for name, fn in JOBS[r.job].items():
        print(f"::group::{name}", flush=True)
        Log.write(f"--- step {name}")
        try:
            status = fn(r) or status
        except Skip as e:
            return f"skipped: {e}"
        except Done as e:
            return str(e)
        except Exception:  # noqa: BLE001 - reported as the job's status
            Log.write(traceback.format_exc())
            return f"ERROR in {name}: {traceback.format_exc().strip().splitlines()[-1]}"
        finally:
            print("::endgroup::", flush=True)
    return status


def cmd_run(args) -> int:
    if args.job not in JOBS:
        raise SystemExit(f"unknown job {args.job!r}; jobs: {', '.join(JOBS)}")
    repo = core.load_repo(os.environ["GITHUB_REPOSITORY"], os.environ.get("AIJ_CONFIG", ""))
    r = Run(job=args.job, repo=repo, live=truthy(args.live))
    if args.issue and args.issue != "0":
        r.data["force_issue"] = int(args.issue)
    Log.open(f"{repo.name}-{r.job}")
    Log.write(f"{r.job} on {repo.slug}, {'LIVE' if r.live else 'dry run'}")
    status = run_steps(r)
    # Giving up on an ai-ok issue labels it ai-stuck, so tomorrow's run doesn't retry it.
    if r.job == "ai-ok" and status.startswith("skipped:") and r.data.get("stuck_on_skip"):
        mark_stuck(repo, r.data["issue"], status.removeprefix("skipped: "), r.live)
    Log.write(status)
    print(f"::{'error' if status.startswith('ERROR') else 'notice'} title={r.job}::{status}")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(f"**{r.job}** ({'live' if r.live else 'dry run'}): {status}\n")
    return 1 if status.startswith("ERROR") else 0


def cmd_gh_app(args) -> int:
    if args.what == "token":
        print(ghapp.token())
    else:
        print("\n".join(ghapp.identity()))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="aij", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("job")
    p.add_argument("--live", default="false")
    p.add_argument("--issue", default="0", help="ai-ok: work on this issue regardless of labels (for trials)")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("gh-app")
    p.add_argument("what", choices=["token", "identity"])
    p.set_defaults(fn=cmd_gh_app)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
