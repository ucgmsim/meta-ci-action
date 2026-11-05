#!/usr/bin/env bash
# Publish this run's plot PNGs to the plots branch and prune what no comment
# can still show, keeping the branch to a single commit.
#
# The sticky comment is edited in place, so it only ever links to its newest
# run: earlier runs' plots for the same PR and package are dead and are
# removed. Plots of PRs closed more than RETENTION_DAYS ago go too. The
# branch is then force-pushed as one parentless commit, so the removed PNGs
# become unreachable and GitHub garbage-collects them instead of the history
# growing with every run. Images are linked by branch and path rather than by
# commit (a pruned commit's raw URL stops resolving); every run writes to a
# fresh path, so raw.githubusercontent's caching cannot serve stale images.
#
# Environment:
#   BRANCH          plots branch name
#   PLOTS_DIR       directory holding this run's *.png
#   SLUG            package slug (one comment per package in a monorepo)
#   PR              PR number, empty for non-PR events
#   RETENTION_DAYS  keep a closed PR's plots this many days (0 = drop at once)
#   GH_TOKEN, GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT,
#   GITHUB_REF_NAME, GITHUB_OUTPUT, RUNNER_TEMP
#   REMOTE_URL      override the push URL (for testing)
#
# Prints nothing on success but writes image-base to GITHUB_OUTPUT.
set -euo pipefail

top="${PR:+pr-$PR}"
top="${top:-$(printf '%s' "$GITHUB_REF_NAME" | tr -c 'A-Za-z0-9._-' '-')}"
dest="$top/$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT/$SLUG"
remote="${REMOTE_URL:-https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git}"
now=$(date +%s)

repo_dir="$RUNNER_TEMP/criterion-plots-repo"
rm -rf "$repo_dir"
git init -q "$repo_dir"
cd "$repo_dir"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git remote add origin "$remote"

# Whether a pr-N directory has outlived the retention window. Unknown PRs
# (API error, deleted) are kept: losing images is worse than a few KB.
expired() {
  local closed_at
  closed_at=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$1" --jq '.closed_at // ""' 2>/dev/null) || return 1
  [ -n "$closed_at" ] || return 1
  [ $(( (now - $(date -d "$closed_at" +%s)) / 86400 )) -ge "$RETENTION_DAYS" ]
}

for attempt in 1 2 3 4 5; do
  rm -rf ./*
  if git fetch -q --depth=1 origin "$BRANCH" 2>/dev/null; then
    tip=$(git rev-parse FETCH_HEAD)
    git checkout -q --detach -f "$tip"
  else
    tip=""  # --force-with-lease with an empty value: branch must not exist yet
    printf '# %s\n\nPlots for criterion benchmark PR comments, written by\n[criterion-bench](https://github.com/ucgmsim/meta-ci-action/tree/main/criterion-bench).\nRewritten as a single commit on every run; only the newest plots per PR are kept.\n' \
      "$BRANCH" > README.md
  fi

  # Earlier runs of this package under the same PR / branch.
  for old in "$top"/*/"$SLUG"; do
    if [ -d "$old" ] && [ "$old" != "$dest" ]; then rm -rf "$old"; fi
  done
  # Closed PRs past retention.
  for dir in pr-*; do
    [ -d "$dir" ] || continue
    n=${dir#pr-}
    [ "$n" = "${PR:-}" ] && continue
    if expired "$n"; then rm -rf "$dir"; fi
  done
  find . -mindepth 1 -type d -empty -not -path './.git' -not -path './.git/*' -delete

  mkdir -p "$dest"
  cp "$PLOTS_DIR"/*.png "$dest"/
  git add -A
  tree=$(git write-tree)
  commit=$(git commit-tree "$tree" -m "Criterion plots (latest: $dest)")
  if git push -q --force-with-lease="refs/heads/$BRANCH:$tip" origin "$commit:refs/heads/$BRANCH"; then
    echo "image-base=https://raw.githubusercontent.com/$GITHUB_REPOSITORY/refs/heads/$BRANCH/$dest" >> "$GITHUB_OUTPUT"
    exit 0
  fi
  # Another run pushed between our fetch and push: redo on top of its tree.
  sleep $((attempt * 3))
done
echo "::warning::Could not push plots to '$BRANCH' (fork PR, or the job lacks contents: write). Posting tables only."
exit 1
