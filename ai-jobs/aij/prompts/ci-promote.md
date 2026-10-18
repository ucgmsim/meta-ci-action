You are improving ucgmsim/meta-ci-action, the team's shared reusable CI
workflows, unattended. Nobody will answer questions. Your output is edits in
this checkout plus two files under .ai/. A script validates with actionlint
and opens the PR.

The CI workflows of every repo that uses (or should use) it are copied
read-only under $consumers_dir/<repo>/.github/workflows/.

$baseline

Find ONE thing that several repos do in their own workflows but
meta-ci-action does not offer, and add it to meta-ci-action so repos can call
it instead. Candidates, in order of value:
- a job or step that two or more repos duplicate (for example a release-mode
  Rust test job, PyPI wheel publishing, a git-dependency pin check, wiki sync);
- an input a repo needs but has to work around by keeping a local copy of a
  shared job;
- a scheduled (cron) run of the default branch.

Rules:
- Changes must be backward compatible. Every existing caller must behave
  exactly as before: new inputs default to off, new jobs are opt-in, new
  reusable workflows go in new files.
- Follow the existing conventions in ci.yml: input descriptions, the
  `detect` job pattern, uv usage, comments explaining why.
- Keep lefthook/ci.yml in step with ci.yml: if you add or change a check a
  developer can run locally, add or change the matching pre-push command
  (same command, tagged, following the file's conventions) and the README's
  hook table. Checks that only make sense in CI (publishing, wiki sync) need
  no hook.
- Update README.md with the new input or workflow, with a usage example.
- Write .ai/pr-title and .ai/pr-body.md. Name the repos that will be able to
  drop their local copy, and say that a v2 tag bump is needed before they
  can use it.

If nothing is worth promoting, write the reason to .ai/skip-reason and stop.
