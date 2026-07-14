# coderabbit-queue

Central, account-wide orchestrator for nudging CodeRabbit (`@coderabbitai`)
on open pull requests across all `blixten85` repos.

## Why this exists

CodeRabbit's review quota (5 reviews/hour) is **account-wide**, not
per-repo. Previously, each repo had its own `coderabbit-rewake.yml`
workflow, independently deciding to nudge CodeRabbit with no visibility
into what any other repo's workflow was doing. Running 13 of these in
parallel blew through the shared quota and caused a full gridlock where
no repo could get a review.

This repo replaces all 13 per-repo `coderabbit-rewake.yml` workflows
with a single cron job (`.github/workflows/orchestrate.yml`) that:

1. Loops over every target repo once per run.
2. For each open PR, decides one action based on priority:
   merge conflict > missing CodeRabbit review > unresolved review threads
   > nothing (already clear, waiting on merge).
3. Tracks every nudge it sends in `queue-state.json`, committed back to
   this repo, so state persists between Action runs.
4. Enforces a strict shared budget of 4 nudges per rolling 60 minutes
   (a safety margin under CodeRabbit's real 5/hour cap), and a 20-minute
   per-PR cooldown so the same PR isn't hammered every cycle.
5. Stops the entire run immediately once the hourly budget is used up,
   saving state so remaining PRs get picked up on the next run.

## Target repos

bastion, scraper, routines-relay, ops-hub, product-describer,
docker-idempotent-update, plex_clear_watchlist, pastebinit,
politiker-kontakter, politiker-webapp, filtered-movies,
product-describer-cloudflare, repo-standard, bastion-certificates,
renovate-runner, secrets-rotation

(Hardcoded in `REPOS` at the top of `orchestrate.py`.)

## Running manually

From GitHub: Actions tab -> "CodeRabbit queue orchestrator" -> Run workflow.

Locally (needs a token with Pull requests read/write across all target
repos, e.g. the same fine-grained PAT stored as `CR_QUEUE_TOKEN`):

```bash
GH_TOKEN=<token> python3 orchestrate.py
```

This will read/write `queue-state.json` in the current directory but will
not commit anything — committing is handled by the workflow step.

## Replacing the old per-repo workflows

Once this is confirmed working, remove `coderabbit-rewake.yml` from each
of the 13 target repos that had it — this repo is the single source of
truth for CodeRabbit nudging going forward.
