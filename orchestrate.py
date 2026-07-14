#!/usr/bin/env python3
"""
Central CodeRabbit nudge orchestrator.

Replaces 13 separate per-repo `coderabbit-rewake.yml` workflows, which had
no visibility into each other and drove CodeRabbit's account-wide 5/hour
review quota into gridlock. This script loops over every target repo once
per invocation, decides the correct next action for each open PR, and
enforces a single shared, self-tracked quota ledger (queue-state.json)
so we never send more than QUOTA_PER_HOUR nudges in any rolling 60
minutes, account-wide.

Requires GH_TOKEN in the environment (a token with Pull requests:
read/write across all target repos) — `gh` and `gh api` pick it up
automatically.
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

OWNER = "blixten85"
REPOS = [
    "bastion",
    "scraper",
    "routines-relay",
    "ops-hub",
    "product-describer",
    "docker-idempotent-update",
    "plex_clear_watchlist",
    "pastebinit",
    "politiker-kontakter",
    "politiker-webapp",
    "filtered-movies",
    "product-describer-cloudflare",
    "repo-standard",
    "bastion-certificates",
    "renovate-runner",
    "secrets-rotation",
]

STATE_FILE = "queue-state.json"
QUOTA_PER_HOUR = 4  # margin under CodeRabbit's real 5/hour account-wide cap
QUOTA_WINDOW_MINUTES = 60
PER_PR_COOLDOWN_MINUTES = 20
MAX_AUTOFIX_ATTEMPTS = 2  # give up and leave for a human after this many tries

NUDGE_MERGE_CONFLICT = "@coderabbitai resolve merge conflict"
NUDGE_REVIEW = "@coderabbitai review"
NUDGE_AUTOFIX = "@coderabbitai autofix"


def now_utc():
    return datetime.now(timezone.utc)


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("nudges", [])  # list of {"ts": iso, "repo": str, "pr": int, "type": str}
    data.setdefault("prs", {})  # "owner/repo#N" -> {"last_attempt": iso}
    return data


def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def prune_nudges(state):
    cutoff = now_utc() - timedelta(minutes=QUOTA_WINDOW_MINUTES)
    state["nudges"] = [n for n in state["nudges"] if parse_ts(n["ts"]) > cutoff]


def quota_remaining(state):
    prune_nudges(state)
    return QUOTA_PER_HOUR - len(state["nudges"])


def record_nudge(state, repo, pr_number, nudge_type):
    ts = now_utc().isoformat()
    state["nudges"].append({"ts": ts, "repo": repo, "pr": pr_number, "type": nudge_type})
    key = f"{OWNER}/{repo}#{pr_number}"
    entry = state["prs"].setdefault(key, {})
    entry["last_attempt"] = ts
    if nudge_type == "autofix":
        entry["autofix_attempts"] = entry.get("autofix_attempts", 0) + 1


def autofix_attempts(state, repo, pr_number):
    key = f"{OWNER}/{repo}#{pr_number}"
    return state["prs"].get(key, {}).get("autofix_attempts", 0)


def recently_attempted(state, repo, pr_number):
    key = f"{OWNER}/{repo}#{pr_number}"
    entry = state["prs"].get(key)
    if not entry or "last_attempt" not in entry:
        return False
    last = parse_ts(entry["last_attempt"])
    return now_utc() - last < timedelta(minutes=PER_PR_COOLDOWN_MINUTES)


def run_gh(args, input_text=None):
    result = subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        input=input_text,
    )
    if result.returncode != 0:
        print(f"gh {' '.join(args)} failed: {result.stderr.strip()}", file=sys.stderr)
        return None
    return result.stdout


def list_open_prs(repo):
    out = run_gh(
        [
            "pr", "list",
            "--repo", f"{OWNER}/{repo}",
            "--state", "open",
            "--json", "number",
            "--limit", "100",
        ]
    )
    if out is None:
        return []
    try:
        return [p["number"] for p in json.loads(out)]
    except json.JSONDecodeError:
        return []


def get_pr_details(repo, number):
    out = run_gh(
        [
            "pr", "view", str(number),
            "--repo", f"{OWNER}/{repo}",
            "--json",
            "mergeStateStatus,mergeable,statusCheckRollup,reviews,comments",
        ]
    )
    if out is None:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def get_unresolved_review_threads(repo, number):
    query = """
    query($owner: String!, $repo: String!, $number: Int!, $endCursor: String) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviewThreads(first: 100, after: $endCursor) {
            nodes { isResolved }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
    """
    unresolved = 0
    cursor = None
    while True:
        args = [
            "api", "graphql",
            "-f", f"query={query}",
            "-f", f"owner={OWNER}",
            "-f", f"repo={repo}",
            "-F", f"number={number}",
        ]
        if cursor:
            args += ["-f", f"endCursor={cursor}"]
        out = run_gh(args)
        if out is None:
            return unresolved
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return unresolved
        threads = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
        )
        nodes = threads.get("nodes", [])
        unresolved += sum(1 for n in nodes if not n.get("isResolved", True))
        page_info = threads.get("pageInfo", {})
        if page_info.get("hasNextPage"):
            cursor = page_info.get("endCursor")
        else:
            break
    return unresolved


def has_coderabbit_check(details):
    rollup = details.get("statusCheckRollup") or []
    for check in rollup:
        name = (check.get("name") or check.get("context") or "")
        if "coderabbit" in name.lower():
            return True
    return False


def has_real_review_comment(details):
    for review in details.get("reviews") or []:
        body = review.get("body") or ""
        author = (review.get("author") or {}).get("login", "")
        if "coderabbit" in author.lower() and body.strip():
            return True
    for comment in details.get("comments") or []:
        author = (comment.get("author") or {}).get("login", "")
        if "coderabbit" in author.lower():
            return True
    return False


def post_comment(repo, number, body):
    result = subprocess.run(
        ["gh", "pr", "comment", str(number), "--repo", f"{OWNER}/{repo}", "--body", body],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Failed to comment on {repo}#{number}: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def process_pr(repo, number, state):
    """Return True if a nudge was sent (consumes quota), False otherwise."""
    if recently_attempted(state, repo, number):
        print(f"  PR #{number}: skipped (nudged within last {PER_PR_COOLDOWN_MINUTES}m)")
        return False

    details = get_pr_details(repo, number)
    if details is None:
        print(f"  PR #{number}: could not fetch details, skipping")
        return False

    mergeable = details.get("mergeable")
    if mergeable == "CONFLICTING":
        print(f"  PR #{number}: merge conflict -> nudging resolve, then leaving alone this run")
        if post_comment(repo, number, NUDGE_MERGE_CONFLICT):
            record_nudge(state, repo, number, "resolve_merge_conflict")
            return True
        return False

    if not has_coderabbit_check(details) or not has_real_review_comment(details):
        print(f"  PR #{number}: no CodeRabbit check/review yet -> nudging review")
        if post_comment(repo, number, NUDGE_REVIEW):
            record_nudge(state, repo, number, "review")
            return True
        return False

    unresolved = get_unresolved_review_threads(repo, number)
    if unresolved > 0:
        attempts = autofix_attempts(state, repo, number)
        if attempts >= MAX_AUTOFIX_ATTEMPTS:
            print(
                f"  PR #{number}: {unresolved} unresolved thread(s) but already tried autofix "
                f"{attempts}x with no resolution -> giving up, needs manual/human intervention"
            )
            return False
        print(f"  PR #{number}: {unresolved} unresolved thread(s), conflict-free -> nudging autofix (attempt {attempts + 1})")
        if post_comment(repo, number, NUDGE_AUTOFIX):
            record_nudge(state, repo, number, "autofix")
            return True
        return False

    print(f"  PR #{number}: all clear, waiting on merge -> no action")
    return False


def main():
    state = load_state()

    for repo in REPOS:
        remaining = quota_remaining(state)
        if remaining <= 0:
            print(f"Global quota exhausted ({QUOTA_PER_HOUR}/hour). Stopping run early.")
            break

        print(f"== {repo} ==")
        pr_numbers = list_open_prs(repo)
        if not pr_numbers:
            print("  no open PRs")
            continue

        for number in pr_numbers:
            remaining = quota_remaining(state)
            if remaining <= 0:
                print(f"Global quota exhausted ({QUOTA_PER_HOUR}/hour). Stopping run early.")
                save_state(state)
                return
            process_pr(repo, number, state)

    save_state(state)
    print("Done. Nudges sent this run recorded in queue-state.json.")


if __name__ == "__main__":
    main()
