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
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import sentry_sdk

sentry_sdk.init(
    dsn=os.environ.get("SENTRY_DSN"),
    send_default_pii=False,
    include_local_variables=False,
    # Tracing
    traces_sample_rate=1.0,
    # Profiling (continuous, trace-lifecycle)
    profile_session_sample_rate=1.0,
    profile_lifecycle="trace",
    # Logs
    enable_logs=True,
)

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
MAX_AUTOFIX_ATTEMPTS = 2  # give up on code fixes after this many tries, then fall back to @resolve
MAX_RESOLVE_ATTEMPTS = 1  # final fallback after autofix is exhausted — only try once
MAX_MERGE_CONFLICT_ATTEMPTS = 2  # give up nudging CodeRabbit's merge-conflict resolver after this many tries
MAX_CUBIC_RETRY_ATTEMPTS = 2  # cubic's own "command failed: Unknown error" — retry this many times, then give up (no spam)

NUDGE_MERGE_CONFLICT = "@coderabbitai resolve merge conflict"
NUDGE_REVIEW = "@coderabbitai review"
NUDGE_AUTOFIX = "@coderabbitai autofix"
NUDGE_RESOLVE = "@coderabbitai resolve"
# Sentry Seer har INGET fix/autofix-kommando (verifierat mot Sentrys egen
# dokumentation, 2026-07-17: docs.sentry.io/product/ai-in-sentry/seer/code-review/
# — bara "@sentry review" och "@sentry generate-test" finns). Sentry-fynd kan
# därför inte nudgas att fixas som CodeRabbit/cubic - de faller igenom till
# den bot-agnostiska @resolve-fallbacken när autofix är uttömt. Men Seer
# hittar riktiga fel/sårbarheter i granskningen, så det är fortfarande värt
# att trigga en granskning om ingen körts än (samma idé som NUDGE_REVIEW
# för CodeRabbit).
NUDGE_SENTRY_REVIEW = "@sentry review"
# Cubic har ett eget, separat autofix-kommando (kör direkt mot PR-branchen,
# se memory reference-cubic-commands.md) — CodeRabbits "@coderabbitai autofix"
# ser BARA sina egna review-kommentarer och skippar tyst annars ("No
# unresolved CodeRabbit review comments with fix instructions found"),
# vilket lämnar cubic-dev-ai-fynd helt onudgade. Ingen egen kvot-gating mot
# QUOTA_PER_HOUR (den är kalibrerad mot CodeRabbits kontobredda 5/timme-tak,
# inte cubics separata gräns).
NUDGE_CUBIC_AUTOFIX = "@cubic-dev-ai fix this issue in this branch"

# CodeRabbits eget svar på en rate-limit-träff, bekräftat ordagrant mot ett
# verkligt konto (2026-07-15): "... More reviews will be available in 21
# minutes." — skiljer sig från vår egen kvot-ledger (queue-state.json), som
# bara är en HEURISTIK baserad på GitHub-events vi själva triggat. Genom att
# läsa CodeRabbits egna kommentarer får vi den FAKTISKA, auktoritativa
# kvotstatusen istället för att gissa och slösa nudgar mot en redan uttömd kvot.
RATE_LIMIT_PATTERN = re.compile(
    r"more reviews will be available in (\d+)\s*(minute|hour)s?", re.IGNORECASE
)

# cubics eget svar när den interna "fix this issue in this branch"-hanteraren
# kraschar innan den ens hinner börja jobba (bekräftat ordagrant 2026-07-17:
# "cubic command failed: Unknown error") — transient, inte en signal om att
# fyndet är ogiltigt. Skiljs från "Working..."/riktiga svar genom att den
# INTE innehåller en progress-länk.
CUBIC_COMMAND_FAILED_PATTERN = re.compile(r"cubic command failed", re.IGNORECASE)


class GhError(Exception):
    """A gh/GitHub API call failed in a way the caller must not paper over.

    Raised for read operations whose empty/partial result would otherwise be
    indistinguishable from a legitimate "nothing here" answer and lead to a
    wrong decision (e.g. treating a PR with unresolved threads as all clear)."""


def now_utc():
    return datetime.now(timezone.utc)


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@sentry_sdk.trace
def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("nudges", [])  # list of {"ts": iso, "repo": str, "pr": int, "type": str}
    data.setdefault("prs", {})  # "owner/repo#N" -> {"last_attempt": iso}
    data.setdefault("rate_limited_until", None)  # iso timestamp, kontobrett

    migrate_merge_conflict_attempts(data)

    return data


def migrate_merge_conflict_attempts(state):
    """Seed merge_conflict_attempts counter from existing nudges to avoid
    resetting attempt counts when the counter was first introduced."""
    merge_conflict_counts = {}
    for nudge in state["nudges"]:
        if nudge.get("type") == "resolve_merge_conflict":
            repo = nudge.get("repo")
            pr = nudge.get("pr")
            if repo and pr:
                key = f"{OWNER}/{repo}#{pr}"
                merge_conflict_counts[key] = merge_conflict_counts.get(key, 0) + 1

    for key, count in merge_conflict_counts.items():
        if key in state["prs"]:
            existing = state["prs"][key].get("merge_conflict_attempts", 0)
            state["prs"][key]["merge_conflict_attempts"] = max(existing, count)


@sentry_sdk.trace
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
    if nudge_type == "resolve":
        entry["resolve_attempts"] = entry.get("resolve_attempts", 0) + 1
    if nudge_type == "resolve_merge_conflict":
        entry["merge_conflict_attempts"] = entry.get("merge_conflict_attempts", 0) + 1
    if nudge_type == "cubic_retry":
        entry["cubic_retry_attempts"] = entry.get("cubic_retry_attempts", 0) + 1


def autofix_attempts(state, repo, pr_number):
    key = f"{OWNER}/{repo}#{pr_number}"
    return state["prs"].get(key, {}).get("autofix_attempts", 0)


def cubic_retry_attempts(state, repo, pr_number):
    key = f"{OWNER}/{repo}#{pr_number}"
    return state["prs"].get(key, {}).get("cubic_retry_attempts", 0)


def resolve_attempts(state, repo, pr_number):
    key = f"{OWNER}/{repo}#{pr_number}"
    return state["prs"].get(key, {}).get("resolve_attempts", 0)


def merge_conflict_attempts(state, repo, pr_number):
    key = f"{OWNER}/{repo}#{pr_number}"
    return state["prs"].get(key, {}).get("merge_conflict_attempts", 0)


def already_escalated(state, repo, pr_number):
    key = f"{OWNER}/{repo}#{pr_number}"
    return bool(state["prs"].get(key, {}).get("escalated_to_claude"))


def mark_escalated(state, repo, pr_number):
    key = f"{OWNER}/{repo}#{pr_number}"
    state["prs"].setdefault(key, {})["escalated_to_claude"] = True


def is_rate_limited(state):
    until = state.get("rate_limited_until")
    if not until:
        return False
    return now_utc() < parse_ts(until)


def detect_and_record_rate_limit(state, details):
    """Skanna PR-kommentarer efter CodeRabbits egen rate-limit-text. Om
    hittad: sätt en kontobred backoff-deadline i state, auktoritativ (från
    CodeRabbit självt) snarare än vår egen händelsebaserade gissning."""
    for comment in details.get("comments") or []:
        author = (comment.get("author") or {}).get("login", "")
        if "coderabbit" not in author.lower():
            continue
        body = comment.get("body") or ""
        m = RATE_LIMIT_PATTERN.search(body)
        if not m:
            continue
        amount, unit = int(m.group(1)), m.group(2).lower()
        seconds = amount * 60 if unit == "minute" else amount * 3600
        deadline = now_utc() + timedelta(seconds=seconds)
        existing = state.get("rate_limited_until")
        if not existing or deadline > parse_ts(existing):
            state["rate_limited_until"] = deadline.isoformat()
            print(f"  CodeRabbit rate limit upptäckt i kommentar -> backar av till {deadline.isoformat()}")
        return True
    return False


def last_cubic_command_failed(details):
    """True om det SENASTE kommentaren på PR:en är cubics eget
    "command failed: Unknown error" — dvs vårt senaste nudge-försök till
    cubic kraschade innan den ens började jobba (skiljer sig från en riktig
    "Working..."-kvittens eller ett faktiskt granskningssvar). Kollar bara
    den absolut sista kommentaren, inte alla cubic-kommentarer historiskt -
    annars skulle en redan-löst gammal krasch trigga nya retries för evigt."""
    comments = details.get("comments") or []
    if not comments:
        return False
    last = comments[-1]
    author = (last.get("author") or {}).get("login", "")
    if "cubic" not in author.lower():
        return False
    body = last.get("body") or ""
    return bool(CUBIC_COMMAND_FAILED_PATTERN.search(body))


def recently_attempted(state, repo, pr_number):
    key = f"{OWNER}/{repo}#{pr_number}"
    entry = state["prs"].get(key)
    if not entry or "last_attempt" not in entry:
        return False
    last = parse_ts(entry["last_attempt"])
    return now_utc() - last < timedelta(minutes=PER_PR_COOLDOWN_MINUTES)


def run_gh(args, input_text=None):
    with sentry_sdk.start_span(name=f"gh {' '.join(args[:3])}") as span:
        span.set_data("gh.args", args)
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            input=input_text,
        )
        span.set_data("gh.returncode", result.returncode)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            span.set_data("gh.stderr", stderr)
            msg = f"gh {' '.join(args)} failed: {stderr}"
            print(msg, file=sys.stderr)
            # Surface the swallowed failure to Sentry instead of only printing
            # to stderr — otherwise a persistent API/auth problem is invisible.
            sentry_sdk.capture_message(msg, level="warning")
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
        # gh failed — NOT the same as "no open PRs". Propagate so the caller
        # skips this repo for the run instead of silently treating it as empty.
        raise GhError(f"could not list open PRs for {OWNER}/{repo}")
    try:
        return [p["number"] for p in json.loads(out)]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise GhError(f"malformed PR list for {OWNER}/{repo}: {e}") from e


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


def get_unresolved_threads_by_author(repo, number):
    """Returnerar (by_author, total_unresolved).

    by_author: olösta, ICKE-utdaterade trådar grupperade per bot som öppnade
    dem (login, gemener) - t.ex. {"coderabbitai": 2, "cubic-dev-ai": 1}.
    Används för att avgöra VILKEN bots autofix-kommando som ska nudgas -
    att be en bot "fixa" en tråd mot en diff som redan ändrats (isOutdated)
    är meningslöst och kan mata in fel kontext, så de exkluderas härifrån.

    total_unresolved: ALLA olösta trådar, INKLUSIVE utdaterade. Utdaterade
    trådar blockerar fortfarande merge (GitHubs required_review_thread_
    resolution-regel bryr sig bara om isResolved, inte isOutdated) - de
    måste därför fortfarande räknas med i gate-logiken (annars tror
    orkestreraren att en PR är "all clear" fast GitHub fortfarande blockerar
    den) och landar till sist i den bot-agnostiska @resolve-fallbacken.
    """
    query = """
    query($owner: String!, $repo: String!, $number: Int!, $endCursor: String) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviewThreads(first: 100, after: $endCursor) {
            nodes {
              isResolved
              isOutdated
              comments(first: 1) { nodes { author { login } } }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
    """
    by_author = {}
    total_unresolved = 0
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
            # Returning the partial counts gathered so far would undercount
            # unresolved threads and could make a still-blocked PR look "all
            # clear" (and get auto-merged). Propagate so the PR is skipped.
            raise GhError(
                f"could not fetch review threads for {OWNER}/{repo}#{number}"
            )
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise GhError(
                f"malformed review-thread response for {OWNER}/{repo}#{number}: {e}"
            ) from e
        threads = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
        )
        nodes = threads.get("nodes", [])
        for n in nodes:
            if n.get("isResolved", True):
                continue
            total_unresolved += 1
            if n.get("isOutdated", False):
                continue
            first_comment = (n.get("comments", {}).get("nodes") or [{}])[0]
            author = ((first_comment.get("author") or {}).get("login") or "unknown").lower()
            by_author[author] = by_author.get(author, 0) + 1
        page_info = threads.get("pageInfo", {})
        if page_info.get("hasNextPage"):
            cursor = page_info.get("endCursor")
        else:
            break
    return by_author, total_unresolved


def get_unresolved_review_threads(repo, number):
    _, total = get_unresolved_threads_by_author(repo, number)
    return total


def has_coderabbit_check(details):
    rollup = details.get("statusCheckRollup") or []
    for check in rollup:
        name = (check.get("name") or check.get("context") or "")
        if "coderabbit" in name.lower():
            return True
    return False


def has_sentry_check(details):
    """Check-namnet är "Seer Code Review" i alla repon vi observerat (inte
    "sentry") - matchar båda orden för att inte missa en framtida
    namnändring åt endera hållet."""
    rollup = details.get("statusCheckRollup") or []
    for check in rollup:
        name = (check.get("name") or check.get("context") or "").lower()
        if "seer" in name or "sentry" in name:
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
        msg = f"Failed to comment on {repo}#{number}: {result.stderr.strip()}"
        print(msg, file=sys.stderr)
        # A dropped nudge silently wastes a run — make it visible in Sentry.
        sentry_sdk.capture_message(msg, level="error")
        return False
    return True


def escalate_to_claude(repo, number):
    """Sista utväg: lägg på `ask-claude`-etiketten (samma mönster som
    claude-assign-trigger.yml redan använder överallt) istället för att bara
    logga och ge upp tyst. ENVÄGS OCH ENGÅNGS med flit — triggas bara av
    GitHubs `labeled`-event, inte textmatchning, så en redan satt etikett
    (t.ex. om orkestreraren råkar köra på samma PR igen) triggar INGET nytt
    event och kostar alltså ingen ny Claude-körning. Två anropsställen når hit
    (resolve-uttömd respektive merge-conflict-uttömd), men already_escalated()
    ovan garanterar ändå max EN eskalering per PR totalt, aldrig en loop —
    samma säkerhetsprincip som förhindrade den tidigare 1500kr/6h-kostnadsincidenten."""
    result = subprocess.run(
        ["gh", "pr", "edit", str(number), "--repo", f"{OWNER}/{repo}", "--add-label", "ask-claude"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = f"Failed to label {repo}#{number} ask-claude: {result.stderr.strip()}"
        print(msg, file=sys.stderr)
        # Escalation is the last resort; if it silently fails the PR is stuck
        # with no one notified — surface it.
        sentry_sdk.capture_message(msg, level="error")
        return False
    return True


def update_branch(repo, number):
    """PUT .../pulls/{number}/update-branch — mergar bas-branchen in i PR-
    branchen (samma sak som GitHubs "Update branch"-knapp). Detta är INTE ett
    CodeRabbit-kommando, men skapar en ny commit som CodeRabbit automatiskt
    granskar på egen hand -> räknas ändå mot vår egen kvot-ledger nedan så vi
    inte råkar trigga fler granskningar än QUOTA_PER_HOUR tillåter totalt."""
    result = subprocess.run(
        ["gh", "api", "-X", "PUT", f"repos/{OWNER}/{repo}/pulls/{number}/update-branch"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = f"Failed to update branch on {repo}#{number}: {result.stderr.strip()}"
        print(msg, file=sys.stderr)
        sentry_sdk.capture_message(msg, level="warning")
        return False
    return True


def get_pr_id_and_automerge(repo, number):
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          id
          autoMergeRequest { enabledAt }
        }
      }
    }
    """
    out = run_gh(
        [
            "api", "graphql",
            "-f", f"query={query}",
            "-f", f"owner={OWNER}",
            "-f", f"repo={repo}",
            "-F", f"number={number}",
        ]
    )
    if out is None:
        return None, False
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None, False
    pr = (data.get("data") or {}).get("repository", {}).get("pullRequest") or {}
    return pr.get("id"), bool(pr.get("autoMergeRequest"))


def enable_auto_merge(repo, pr_id):
    mutation = """
    mutation($id: ID!) {
      enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: SQUASH}) {
        clientMutationId
      }
    }
    """
    out = run_gh(["api", "graphql", "-f", f"query={mutation}", "-f", f"id={pr_id}"])
    if out is None:
        print(f"Failed to enable auto-merge on {repo}: {pr_id}", file=sys.stderr)
        return False
    return True


def process_pr(repo, number, state):
    """Return True if a nudge was sent (consumes quota), False otherwise."""
    details = get_pr_details(repo, number)
    if details is None:
        print(f"  PR #{number}: could not fetch details, skipping")
        return False

    # cubics egen "command failed"-krasch kollas FÖRE den vanliga
    # PER_PR_COOLDOWN_MINUTES-spärren nedan — annars hade en transient
    # krasch tvingat oss vänta 20 minuter innan vi ens fick FÖRSÖKA igen.
    # Säkert att köra tidigt ändå: gated på att SENASTE kommentaren
    # faktiskt är felet (inte en gissning) OCH ett hårt tak
    # (MAX_CUBIC_RETRY_ATTEMPTS) så det aldrig blir en spam-loop - lyckas
    # cubic (eller vi ger upp efter taket), slutar den senaste kommentaren
    # matcha mönstret och den här grenen triggar aldrig om.
    if last_cubic_command_failed(details):
        retries = cubic_retry_attempts(state, repo, number)
        if retries < MAX_CUBIC_RETRY_ATTEMPTS:
            print(f"  PR #{number}: cubic \"command failed\" upptäckt -> försöker igen (retry {retries + 1}/{MAX_CUBIC_RETRY_ATTEMPTS})")
            if post_comment(repo, number, NUDGE_CUBIC_AUTOFIX):
                record_nudge(state, repo, number, "cubic_retry")
                return True
            return False
        print(f"  PR #{number}: cubic \"command failed\" kvarstår efter {retries} retry-försök -> ger upp, ingen mer spam")
        return False

    if recently_attempted(state, repo, number):
        print(f"  PR #{number}: skipped (nudged within last {PER_PR_COOLDOWN_MINUTES}m)")
        return False

    # Läs CodeRabbits egna kommentarer efter en rate-limit-signal INNAN vi
    # bestämmer nästa åtgärd — den är auktoritativ (från CodeRabbit självt),
    # vår egen ledger är bara en gissning baserad på events vi triggat.
    detect_and_record_rate_limit(state, details)
    if is_rate_limited(state):
        print(f"  PR #{number}: CodeRabbit rate-limited (kontobrett) -> hoppar över alla granskningsnudgar")
        # Auto-merge-aktivering kostar ingen granskningskvot — säkert oavsett.
        pr_id, has_automerge = get_pr_id_and_automerge(repo, number)
        if pr_id and not has_automerge:
            enable_auto_merge(repo, pr_id)
        return False

    mergeable = details.get("mergeable")
    if mergeable == "CONFLICTING":
        attempts = merge_conflict_attempts(state, repo, number)
        if attempts >= MAX_MERGE_CONFLICT_ATTEMPTS:
            if already_escalated(state, repo, number):
                print(f"  PR #{number}: merge conflict, redan eskalerad till @claude tidigare -> ingen ny åtgärd")
                return False
            print(
                f"  PR #{number}: merge conflict kvarstår efter {attempts} @resolve-nudgar -> "
                f"eskalerar till @claude (ask-claude-etikett, engångs) istället för att nudga vidare"
            )
            if escalate_to_claude(repo, number):
                mark_escalated(state, repo, number)
            return False
        print(f"  PR #{number}: merge conflict -> nudging resolve (attempt {attempts + 1}), then leaving alone this run")
        if post_comment(repo, number, NUDGE_MERGE_CONFLICT):
            record_nudge(state, repo, number, "resolve_merge_conflict")
            return True
        return False

    # Branchen ligger efter bas-branchen (GitHubs "Update branch"-knapp) —
    # utan detta blir en annars klar PR aldrig mergbar. Detta är inget
    # CodeRabbit-kommando, men skapar en ny commit som CodeRabbit granskar
    # automatiskt -> räknas mot vår kvot-ledger som en "nudge" ändå.
    if details.get("mergeStateStatus") == "BEHIND":
        print(f"  PR #{number}: efter bas-branchen -> uppdaterar branchen")
        if update_branch(repo, number):
            record_nudge(state, repo, number, "update_branch")
            return True
        return False

    needs_coderabbit_review = not has_coderabbit_check(details) or not has_real_review_comment(details)
    needs_sentry_review = not has_sentry_check(details)
    if needs_coderabbit_review or needs_sentry_review:
        posted_any = False
        if needs_coderabbit_review:
            print(f"  PR #{number}: no CodeRabbit check/review yet -> nudging review")
            posted_any = post_comment(repo, number, NUDGE_REVIEW) or posted_any
        if needs_sentry_review:
            print(f"  PR #{number}: no Seer/Sentry check yet -> nudging @sentry review")
            posted_any = post_comment(repo, number, NUDGE_SENTRY_REVIEW) or posted_any
        if posted_any:
            record_nudge(state, repo, number, "review")
            return True
        return False

    by_author, unresolved = get_unresolved_threads_by_author(repo, number)
    if unresolved > 0:
        attempts = autofix_attempts(state, repo, number)
        actionable = by_author.get("coderabbitai", 0) > 0 or by_author.get("cubic-dev-ai", 0) > 0
        other = unresolved - by_author.get("coderabbitai", 0) - by_author.get("cubic-dev-ai", 0)
        # Om INGET är nudgningsbart (bara utdaterade trådar och/eller bottar
        # utan känt autofix-kommando) hoppar vi förbi autofix-försöken helt
        # och går direkt till @resolve-fallbacken nedan - annars skulle
        # attempts aldrig öka (record_nudge körs bara vid posted_any) och PR:en
        # fastnar i en oändlig "ingen åtgärd"-loop som aldrig löser sig.
        if actionable and attempts < MAX_AUTOFIX_ATTEMPTS:
            # Varje bot fixar bara sina EGNA trådar när man nudgar den -
            # CodeRabbits "@coderabbitai autofix" skippar tyst om alla olösta
            # trådar kommer från cubic-dev-ai/sentry (exakt felet som
            # motiverade den här uppdelningen). Nudga alla bottar som
            # faktiskt har olösta, ICKE-utdaterade trådar, i samma körning.
            posted_any = False
            if by_author.get("coderabbitai", 0) > 0:
                print(f"  PR #{number}: {by_author['coderabbitai']} olöst CodeRabbit-tråd(ar) -> nudging autofix (attempt {attempts + 1})")
                posted_any = post_comment(repo, number, NUDGE_AUTOFIX) or posted_any
            if by_author.get("cubic-dev-ai", 0) > 0:
                print(f"  PR #{number}: {by_author['cubic-dev-ai']} olöst cubic-tråd(ar) -> nudging cubic fix")
                posted_any = post_comment(repo, number, NUDGE_CUBIC_AUTOFIX) or posted_any
            if other > 0:
                print(f"  PR #{number}: {other} olöst tråd(ar) utan känt autofix-kommando (utdaterade och/eller övriga bottar som sentry) -> ingen nudge för dem, faller vidare till @resolve när autofix uttöms")
            if posted_any:
                record_nudge(state, repo, number, "autofix")
                return True
            return False

        # Autofix gav upp — sista fallback: `@coderabbitai resolve` löser
        # CodeRabbits EGNA kvarvarande trådar (bekräftat mot CodeRabbits
        # dokumentation: kommandot rör bara dess egna kommentarer, aldrig
        # cubic-dev-ai/Sentry/andras). RISK, medvetet accepterad: @resolve
        # verifierar inte att koden faktiskt är fixad, den stänger bara
        # konversationerna — ett CodeRabbit-fynd kan mergas OGRANSKAT av en
        # människa om autofix aldrig lyckades fixa det på riktigt. Trådar
        # från cubic-dev-ai eller andra bottar/människor rörs INTE av detta
        # kommando och förblir olösta (blockerar merge tills de hanteras
        # separat). Körs bara EN gång per PR (MAX_RESOLVE_ATTEMPTS).
        resolved_tries = resolve_attempts(state, repo, number)
        if resolved_tries >= MAX_RESOLVE_ATTEMPTS:
            if already_escalated(state, repo, number):
                print(f"  PR #{number}: redan eskalerad till @claude tidigare -> ingen ny åtgärd")
                return False
            print(
                f"  PR #{number}: {unresolved} unresolved thread(s), autofix ({attempts}x) OCH resolve redan försökt "
                f"utan effekt -> eskalerar till @claude (ask-claude-etikett, engångs)"
            )
            if escalate_to_claude(repo, number):
                mark_escalated(state, repo, number)
            return False
        print(
            f"  PR #{number}: {unresolved} unresolved thread(s), autofix uttömt ({attempts}x) -> "
            f"tvingar grönt med @resolve (full automatisering, ingen severity-spärr)"
        )
        if post_comment(repo, number, NUDGE_RESOLVE):
            record_nudge(state, repo, number, "resolve")
            return True
        return False

    # Allt klart (inga trådar, ingen konflikt, inte efter bas-branchen) men
    # kan ändå fastna om auto-merge-flaggan aldrig aktiverats — ingen ny
    # granskning triggas av detta (bara metadata), så kostar ingen kvot.
    pr_id, has_automerge = get_pr_id_and_automerge(repo, number)
    if pr_id and not has_automerge:
        print(f"  PR #{number}: allt klart men auto-merge ej aktiverat -> aktiverar")
        enable_auto_merge(repo, pr_id)
        return False

    print(f"  PR #{number}: all clear, waiting on merge -> no action")
    return False


def main():
    with sentry_sdk.start_transaction(op="task", name="orchestrator-run"):
        state = load_state()

        sentry_sdk.logger.info(
            "Orchestrator run started for {repo_count} repositories",
            repo_count=len(REPOS),
        )

        # Persist state no matter how the run ends. record_nudge() mutates
        # `state` right after a nudge is actually posted to GitHub; if an
        # unexpected error aborted the run before we saved, those already-sent
        # nudges would vanish from the ledger and be re-sent next run — blowing
        # the account-wide quota this orchestrator exists to protect.
        try:
            _run(state)
        finally:
            save_state(state)

        sentry_sdk.logger.info("Orchestrator run completed")
        print("Done. Nudges sent this run recorded in queue-state.json.")


def _run(state):
    for repo in REPOS:
        remaining = quota_remaining(state)
        if remaining <= 0:
            sentry_sdk.logger.warning(
                "Global quota exhausted ({quota}/hour). Stopping run early.",
                quota=QUOTA_PER_HOUR,
            )
            print(f"Global quota exhausted ({QUOTA_PER_HOUR}/hour). Stopping run early.")
            return

        print(f"== {repo} ==")
        sentry_sdk.set_tag("github.repo", f"{OWNER}/{repo}")

        with sentry_sdk.start_span(name=f"process_repo {repo}") as repo_span:
            repo_span.set_data("github.repo", f"{OWNER}/{repo}")
            try:
                pr_numbers = list_open_prs(repo)
            except GhError as e:
                # Couldn't enumerate PRs — skip this repo for the run rather
                # than mistaking the API failure for "no open PRs".
                print(f"  skipping {repo}: {e}", file=sys.stderr)
                sentry_sdk.capture_exception(e)
                continue
            if not pr_numbers:
                print("  no open PRs")
                continue

            for number in pr_numbers:
                remaining = quota_remaining(state)
                if remaining <= 0:
                    sentry_sdk.logger.warning(
                        "Global quota exhausted ({quota}/hour). Stopping run early.",
                        quota=QUOTA_PER_HOUR,
                    )
                    print(f"Global quota exhausted ({QUOTA_PER_HOUR}/hour). Stopping run early.")
                    return
                with sentry_sdk.start_span(name=f"process_pr {repo}#{number}") as pr_span:
                    pr_span.set_data("github.repo", f"{OWNER}/{repo}")
                    pr_span.set_data("github.pr_number", number)
                    # Isolate per-PR failures: one bad PR must not abort the
                    # whole run (and lose the state of every nudge already
                    # sent this run). Report it and move on.
                    try:
                        process_pr(repo, number, state)
                    except Exception as e:
                        print(f"  {repo}#{number}: error, skipping: {e}", file=sys.stderr)
                        sentry_sdk.capture_exception(e)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sentry_sdk.capture_exception(e)
        event_id = sentry_sdk.last_event_id()
        if event_id and sys.stdin.isatty():
            notes = input("Describe what you were doing when this failed: ")
            sentry_sdk.capture_user_feedback({
                "event_id": event_id,
                "name": "Operator",
                "email": "",
                "comments": notes,
            })
        raise
    finally:
        sentry_sdk.flush(timeout=5)
