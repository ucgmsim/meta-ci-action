"""GitHub as the bot's GitHub App: short-lived installation tokens minted from the app's key.

The workflow passes the app's ID and private key in AIJ_GH_APP_ID and AIJ_GH_APP_KEY (org
secrets). Installation tokens last an hour and a job can run for several, so bin/gh asks
`aij gh-app token` for one on every call; it's cached and re-minted when under ten minutes
are left. Claude's session gets neither the key nor a token (see core.run_claude).
"""

import datetime as dt
import functools
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import jwt

API = "https://api.github.com"
MIN_LEFT = dt.timedelta(minutes=10)
CACHE = Path(os.environ.get("AIJ_DATA", "/tmp/aij")) / "github-app-token.json"


def api(method: str, path: str, token: str) -> dict:
    req = urllib.request.Request(f"{API}/{path}", method=method, headers={
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    })  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"GitHub {method} /{path}: {e.code} {e.read().decode(errors='replace').strip()}") from e


def app_jwt() -> str:
    """A ten-minute JWT signed with the app's key, which only lets it ask for installation tokens."""
    now = int(time.time())
    key = os.environ["AIJ_GH_APP_KEY"]
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": os.environ["AIJ_GH_APP_ID"]}, key, algorithm="RS256")


def token() -> str:
    """An installation token for the workflow's repo with at least MIN_LEFT to run."""
    if CACHE.exists():
        cached = json.loads(CACHE.read_text())
        if dt.datetime.fromisoformat(cached["expires_at"]) - dt.datetime.now(dt.UTC) > MIN_LEFT:
            return cached["token"]
    # Every repo the app is installed on in the org shares one installation, so the token
    # also reaches meta-ci-action (ci-conformance) and the other repos (ci-promote).
    installation = api("GET", f"repos/{os.environ['GITHUB_REPOSITORY']}/installation", app_jwt())["id"]
    out = api("POST", f"app/installations/{installation}/access_tokens", app_jwt())
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.touch(mode=0o600)
    CACHE.write_text(json.dumps({"token": out["token"], "expires_at": out["expires_at"].replace("Z", "+00:00")}))
    return out["token"]


@functools.cache
def slug() -> str:
    return api("GET", "app", app_jwt())["slug"]


def author() -> str:
    """The bot as `gh pr list --author` wants it."""
    return f"app/{slug()}"


def identity() -> tuple[str, str]:
    """Git's name and email for the bot, so commits show as the app (e.g. ucgmsim[bot]) with its avatar."""
    name = f"{slug()}[bot]"
    user = api("GET", f"users/{name.replace('[', '%5B').replace(']', '%5D')}", token())
    return name, f"{user['id']}+{name}@users.noreply.github.com"
