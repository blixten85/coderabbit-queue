"""Unit tests for orchestrate.py.

The repo ships a single module (``orchestrate.py``) with no prior test
coverage. These tests exercise the state ledger, quota accounting, comment
parsing, GitHub helper wrappers (with ``subprocess`` mocked) and the
``process_pr`` decision tree.
"""
import json
import subprocess
from datetime import timedelta
from unittest import mock

import pytest

import orchestrate


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def test_now_utc_is_timezone_aware():
    now = orchestrate.now_utc()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_parse_ts_handles_zulu_suffix():
    parsed = orchestrate.parse_ts("2026-07-21T16:00:00Z")
    assert parsed.tzinfo is not None
    assert parsed.year == 2026 and parsed.hour == 16


def test_parse_ts_handles_explicit_offset():
    parsed = orchestrate.parse_ts("2026-07-21T16:00:00+00:00")
    assert parsed.utcoffset() == timedelta(0)


# --------------------------------------------------------------------------
# State load / save / migrate
# --------------------------------------------------------------------------
@pytest.fixture
def in_state_dir(tmp_path, monkeypatch):
    """Run each state test in an isolated cwd so STATE_FILE is scoped."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_load_state_missing_file_returns_defaults(in_state_dir):
    state = orchestrate.load_state()
    assert state == {"nudges": [], "prs": {}, "rate_limited_until": None}


def test_load_state_invalid_json_returns_defaults(in_state_dir):
    (in_state_dir / orchestrate.STATE_FILE).write_text("{not json")
    state = orchestrate.load_state()
    assert state["nudges"] == []
    assert state["prs"] == {}
    assert state["rate_limited_until"] is None


def test_load_state_preserves_existing_fields(in_state_dir):
    payload = {
        "nudges": [{"ts": "2026-07-21T15:00:00+00:00", "repo": "bastion", "pr": 1, "type": "review"}],
        "prs": {"blixten85/bastion#1": {"last_attempt": "2026-07-21T15:00:00+00:00"}},
        "rate_limited_until": "2026-07-21T17:00:00+00:00",
    }
    (in_state_dir / orchestrate.STATE_FILE).write_text(json.dumps(payload))
    state = orchestrate.load_state()
    assert state["rate_limited_until"] == "2026-07-21T17:00:00+00:00"
    assert len(state["nudges"]) == 1


def test_save_state_round_trips(in_state_dir):
    state = {"nudges": [], "prs": {"x": {"a": 1}}, "rate_limited_until": None}
    orchestrate.save_state(state)
    content = (in_state_dir / orchestrate.STATE_FILE).read_text()
    assert content.endswith("\n")
    assert json.loads(content) == state


def test_migrate_merge_conflict_attempts_seeds_counter():
    state = {
        "nudges": [
            {"type": "resolve_merge_conflict", "repo": "bastion", "pr": 5},
            {"type": "resolve_merge_conflict", "repo": "bastion", "pr": 5},
            {"type": "review", "repo": "bastion", "pr": 5},
        ],
        "prs": {"blixten85/bastion#5": {}},
    }
    orchestrate.migrate_merge_conflict_attempts(state)
    assert state["prs"]["blixten85/bastion#5"]["merge_conflict_attempts"] == 2


def test_migrate_merge_conflict_attempts_does_not_lower_existing():
    state = {
        "nudges": [{"type": "resolve_merge_conflict", "repo": "bastion", "pr": 5}],
        "prs": {"blixten85/bastion#5": {"merge_conflict_attempts": 9}},
    }
    orchestrate.migrate_merge_conflict_attempts(state)
    assert state["prs"]["blixten85/bastion#5"]["merge_conflict_attempts"] == 9


def test_migrate_merge_conflict_attempts_skips_unknown_pr():
    state = {
        "nudges": [{"type": "resolve_merge_conflict", "repo": "bastion", "pr": 5}],
        "prs": {},
    }
    orchestrate.migrate_merge_conflict_attempts(state)
    assert state["prs"] == {}


# --------------------------------------------------------------------------
# Quota accounting
# --------------------------------------------------------------------------
def _blank_state():
    return {"nudges": [], "prs": {}, "rate_limited_until": None}


def test_prune_nudges_drops_old_entries():
    state = _blank_state()
    old = (orchestrate.now_utc() - timedelta(minutes=90)).isoformat()
    fresh = orchestrate.now_utc().isoformat()
    state["nudges"] = [{"ts": old, "type": "review"}, {"ts": fresh, "type": "review"}]
    orchestrate.prune_nudges(state)
    assert len(state["nudges"]) == 1
    assert state["nudges"][0]["ts"] == fresh


def test_quota_remaining_reflects_recent_nudges():
    state = _blank_state()
    now = orchestrate.now_utc().isoformat()
    state["nudges"] = [{"ts": now, "type": "review"}, {"ts": now, "type": "autofix"}]
    assert orchestrate.quota_remaining(state) == orchestrate.QUOTA_PER_HOUR - 2


def test_quota_remaining_ignores_expired_nudges():
    state = _blank_state()
    old = (orchestrate.now_utc() - timedelta(minutes=120)).isoformat()
    state["nudges"] = [{"ts": old, "type": "review"}]
    assert orchestrate.quota_remaining(state) == orchestrate.QUOTA_PER_HOUR


# --------------------------------------------------------------------------
# record_nudge and per-type counters
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "nudge_type,counter,reader",
    [
        ("autofix", "autofix_attempts", orchestrate.autofix_attempts),
        ("resolve", "resolve_attempts", orchestrate.resolve_attempts),
        ("resolve_merge_conflict", "merge_conflict_attempts", orchestrate.merge_conflict_attempts),
        ("cubic_retry", "cubic_retry_attempts", orchestrate.cubic_retry_attempts),
    ],
)
def test_record_nudge_increments_counter(nudge_type, counter, reader):
    state = _blank_state()
    orchestrate.record_nudge(state, "bastion", 7, nudge_type)
    key = "blixten85/bastion#7"
    assert state["prs"][key][counter] == 1
    assert "last_attempt" in state["prs"][key]
    assert reader(state, "bastion", 7) == 1
    assert len(state["nudges"]) == 1


def test_record_nudge_review_sets_no_counter():
    state = _blank_state()
    orchestrate.record_nudge(state, "bastion", 7, "review")
    entry = state["prs"]["blixten85/bastion#7"]
    assert "autofix_attempts" not in entry
    assert entry["last_attempt"]


def test_counter_readers_default_zero_when_absent():
    state = _blank_state()
    assert orchestrate.autofix_attempts(state, "x", 1) == 0
    assert orchestrate.resolve_attempts(state, "x", 1) == 0
    assert orchestrate.merge_conflict_attempts(state, "x", 1) == 0
    assert orchestrate.cubic_retry_attempts(state, "x", 1) == 0


# --------------------------------------------------------------------------
# Escalation flags
# --------------------------------------------------------------------------
def test_escalation_flag_lifecycle():
    state = _blank_state()
    assert orchestrate.already_escalated(state, "bastion", 3) is False
    orchestrate.mark_escalated(state, "bastion", 3)
    assert orchestrate.already_escalated(state, "bastion", 3) is True


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------
def test_is_rate_limited_none():
    assert orchestrate.is_rate_limited(_blank_state()) is False


def test_is_rate_limited_future():
    state = _blank_state()
    state["rate_limited_until"] = (orchestrate.now_utc() + timedelta(minutes=10)).isoformat()
    assert orchestrate.is_rate_limited(state) is True


def test_is_rate_limited_past():
    state = _blank_state()
    state["rate_limited_until"] = (orchestrate.now_utc() - timedelta(minutes=10)).isoformat()
    assert orchestrate.is_rate_limited(state) is False


def test_detect_and_record_rate_limit_minutes():
    state = _blank_state()
    details = {
        "comments": [
            {"author": {"login": "coderabbitai[bot]"},
             "body": "Note: More reviews will be available in 21 minutes."}
        ]
    }
    assert orchestrate.detect_and_record_rate_limit(state, details) is True
    assert orchestrate.is_rate_limited(state) is True


def test_detect_and_record_rate_limit_hours():
    state = _blank_state()
    details = {
        "comments": [
            {"author": {"login": "coderabbitai"},
             "body": "More reviews will be available in 1 hour."}
        ]
    }
    assert orchestrate.detect_and_record_rate_limit(state, details) is True
    deadline = orchestrate.parse_ts(state["rate_limited_until"])
    assert deadline > orchestrate.now_utc() + timedelta(minutes=55)


def test_detect_and_record_rate_limit_keeps_later_deadline():
    state = _blank_state()
    far = (orchestrate.now_utc() + timedelta(hours=5)).isoformat()
    state["rate_limited_until"] = far
    details = {
        "comments": [
            {"author": {"login": "coderabbitai"},
             "body": "More reviews will be available in 5 minutes."}
        ]
    }
    orchestrate.detect_and_record_rate_limit(state, details)
    assert state["rate_limited_until"] == far


def test_detect_and_record_rate_limit_ignores_non_coderabbit():
    state = _blank_state()
    details = {
        "comments": [
            {"author": {"login": "someuser"},
             "body": "More reviews will be available in 30 minutes."}
        ]
    }
    assert orchestrate.detect_and_record_rate_limit(state, details) is False
    assert state["rate_limited_until"] is None


def test_detect_and_record_rate_limit_no_comments():
    state = _blank_state()
    assert orchestrate.detect_and_record_rate_limit(state, {"comments": None}) is False


# --------------------------------------------------------------------------
# cubic "command failed" detection
# --------------------------------------------------------------------------
def test_last_cubic_command_failed_true():
    details = {"comments": [
        {"author": {"login": "coderabbitai"}, "body": "review"},
        {"author": {"login": "cubic-dev-ai[bot]"}, "body": "cubic command failed: Unknown error"},
    ]}
    assert orchestrate.last_cubic_command_failed(details) is True


def test_last_cubic_command_failed_only_checks_last_comment():
    details = {"comments": [
        {"author": {"login": "cubic-dev-ai"}, "body": "cubic command failed: Unknown error"},
        {"author": {"login": "cubic-dev-ai"}, "body": "Working... [progress](http://x)"},
    ]}
    assert orchestrate.last_cubic_command_failed(details) is False


def test_last_cubic_command_failed_non_cubic_last():
    details = {"comments": [
        {"author": {"login": "coderabbitai"}, "body": "cubic command failed"},
    ]}
    assert orchestrate.last_cubic_command_failed(details) is False


def test_last_cubic_command_failed_empty():
    assert orchestrate.last_cubic_command_failed({"comments": []}) is False
    assert orchestrate.last_cubic_command_failed({}) is False


# --------------------------------------------------------------------------
# Cooldown
# --------------------------------------------------------------------------
def test_recently_attempted_true_within_window():
    state = _blank_state()
    state["prs"]["blixten85/bastion#1"] = {"last_attempt": orchestrate.now_utc().isoformat()}
    assert orchestrate.recently_attempted(state, "bastion", 1) is True


def test_recently_attempted_false_outside_window():
    state = _blank_state()
    old = (orchestrate.now_utc() - timedelta(minutes=30)).isoformat()
    state["prs"]["blixten85/bastion#1"] = {"last_attempt": old}
    assert orchestrate.recently_attempted(state, "bastion", 1) is False


def test_recently_attempted_missing_entry():
    assert orchestrate.recently_attempted(_blank_state(), "bastion", 1) is False


# --------------------------------------------------------------------------
# Review / check detection
# --------------------------------------------------------------------------
def test_has_coderabbit_check_true():
    details = {"statusCheckRollup": [{"name": "CodeRabbit"}]}
    assert orchestrate.has_coderabbit_check(details) is True


def test_has_coderabbit_check_context_field():
    details = {"statusCheckRollup": [{"context": "coderabbit-review"}]}
    assert orchestrate.has_coderabbit_check(details) is True


def test_has_coderabbit_check_false():
    assert orchestrate.has_coderabbit_check({"statusCheckRollup": [{"name": "ci"}]}) is False
    assert orchestrate.has_coderabbit_check({"statusCheckRollup": None}) is False


def test_has_sentry_check_matches_seer_and_sentry():
    assert orchestrate.has_sentry_check({"statusCheckRollup": [{"name": "Seer Code Review"}]}) is True
    assert orchestrate.has_sentry_check({"statusCheckRollup": [{"context": "sentry"}]}) is True


def test_has_sentry_check_false():
    assert orchestrate.has_sentry_check({"statusCheckRollup": [{"name": "ci"}]}) is False


def test_has_real_review_comment_from_review_body():
    details = {"reviews": [{"author": {"login": "coderabbitai"}, "body": "Looks good"}]}
    assert orchestrate.has_real_review_comment(details) is True


def test_has_real_review_comment_ignores_empty_review_body():
    details = {"reviews": [{"author": {"login": "coderabbitai"}, "body": "   "}], "comments": []}
    assert orchestrate.has_real_review_comment(details) is False


def test_has_real_review_comment_from_comment():
    details = {"reviews": [], "comments": [{"author": {"login": "coderabbitai[bot]"}}]}
    assert orchestrate.has_real_review_comment(details) is True


def test_has_real_review_comment_false():
    details = {"reviews": [], "comments": [{"author": {"login": "human"}}]}
    assert orchestrate.has_real_review_comment(details) is False


# --------------------------------------------------------------------------
# Regex patterns
# --------------------------------------------------------------------------
def test_rate_limit_pattern_case_insensitive():
    m = orchestrate.RATE_LIMIT_PATTERN.search("MORE REVIEWS WILL BE AVAILABLE IN 3 HOURS")
    assert m and m.group(1) == "3" and m.group(2).lower() == "hour"


def test_cubic_command_failed_pattern():
    assert orchestrate.CUBIC_COMMAND_FAILED_PATTERN.search("cubic command failed: Unknown error")
    assert not orchestrate.CUBIC_COMMAND_FAILED_PATTERN.search("all good")


# --------------------------------------------------------------------------
# subprocess-backed gh helpers
# --------------------------------------------------------------------------
def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_gh_success():
    with mock.patch("orchestrate.subprocess.run", return_value=_completed(stdout="hi")) as run:
        assert orchestrate.run_gh(["pr", "list"]) == "hi"
        run.assert_called_once()
        assert run.call_args.args[0][0] == "gh"


def test_run_gh_failure_returns_none():
    with mock.patch("orchestrate.subprocess.run", return_value=_completed(returncode=1, stderr="boom")):
        assert orchestrate.run_gh(["pr", "list"]) is None


def test_list_open_prs_parses_numbers():
    with mock.patch("orchestrate.run_gh", return_value=json.dumps([{"number": 1}, {"number": 2}])):
        assert orchestrate.list_open_prs("bastion") == [1, 2]


def test_list_open_prs_none_output():
    with mock.patch("orchestrate.run_gh", return_value=None):
        assert orchestrate.list_open_prs("bastion") == []


def test_list_open_prs_bad_json():
    with mock.patch("orchestrate.run_gh", return_value="not json"):
        assert orchestrate.list_open_prs("bastion") == []


def test_get_pr_details_parses_json():
    with mock.patch("orchestrate.run_gh", return_value='{"mergeable": "MERGEABLE"}'):
        assert orchestrate.get_pr_details("bastion", 1) == {"mergeable": "MERGEABLE"}


def test_get_pr_details_none_and_bad_json():
    with mock.patch("orchestrate.run_gh", return_value=None):
        assert orchestrate.get_pr_details("bastion", 1) is None
    with mock.patch("orchestrate.run_gh", return_value="{bad"):
        assert orchestrate.get_pr_details("bastion", 1) is None


def test_post_comment_success_and_failure():
    with mock.patch("orchestrate.subprocess.run", return_value=_completed()):
        assert orchestrate.post_comment("bastion", 1, "hi") is True
    with mock.patch("orchestrate.subprocess.run", return_value=_completed(returncode=1, stderr="x")):
        assert orchestrate.post_comment("bastion", 1, "hi") is False


def test_escalate_to_claude_success_and_failure():
    with mock.patch("orchestrate.subprocess.run", return_value=_completed()):
        assert orchestrate.escalate_to_claude("bastion", 1) is True
    with mock.patch("orchestrate.subprocess.run", return_value=_completed(returncode=1, stderr="x")):
        assert orchestrate.escalate_to_claude("bastion", 1) is False


def test_update_branch_success_and_failure():
    with mock.patch("orchestrate.subprocess.run", return_value=_completed()):
        assert orchestrate.update_branch("bastion", 1) is True
    with mock.patch("orchestrate.subprocess.run", return_value=_completed(returncode=1, stderr="x")):
        assert orchestrate.update_branch("bastion", 1) is False


# --------------------------------------------------------------------------
# GraphQL-backed helpers
# --------------------------------------------------------------------------
def _threads_page(nodes, has_next=False, cursor=None):
    return json.dumps({
        "data": {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        }}}}
    })


def test_get_unresolved_threads_by_author_groups_and_totals():
    nodes = [
        {"isResolved": False, "isOutdated": False, "comments": {"nodes": [{"author": {"login": "coderabbitai"}}]}},
        {"isResolved": False, "isOutdated": False, "comments": {"nodes": [{"author": {"login": "cubic-dev-ai"}}]}},
        {"isResolved": False, "isOutdated": True, "comments": {"nodes": [{"author": {"login": "coderabbitai"}}]}},
        {"isResolved": True, "isOutdated": False, "comments": {"nodes": [{"author": {"login": "coderabbitai"}}]}},
    ]
    with mock.patch("orchestrate.run_gh", return_value=_threads_page(nodes)):
        by_author, total = orchestrate.get_unresolved_threads_by_author("bastion", 1)
    assert total == 3  # two fresh + one outdated, resolved excluded
    assert by_author == {"coderabbitai": 1, "cubic-dev-ai": 1}


def test_get_unresolved_threads_paginates():
    page1 = _threads_page(
        [{"isResolved": False, "isOutdated": False,
          "comments": {"nodes": [{"author": {"login": "coderabbitai"}}]}}],
        has_next=True, cursor="CUR",
    )
    page2 = _threads_page(
        [{"isResolved": False, "isOutdated": False,
          "comments": {"nodes": [{"author": {"login": "cubic-dev-ai"}}]}}],
    )
    with mock.patch("orchestrate.run_gh", side_effect=[page1, page2]) as run:
        by_author, total = orchestrate.get_unresolved_threads_by_author("bastion", 1)
    assert run.call_count == 2
    assert total == 2
    assert by_author == {"coderabbitai": 1, "cubic-dev-ai": 1}


def test_get_unresolved_threads_run_gh_none():
    with mock.patch("orchestrate.run_gh", return_value=None):
        assert orchestrate.get_unresolved_threads_by_author("bastion", 1) == ({}, 0)


def test_get_unresolved_threads_bad_json():
    with mock.patch("orchestrate.run_gh", return_value="nope"):
        assert orchestrate.get_unresolved_threads_by_author("bastion", 1) == ({}, 0)


def test_get_unresolved_review_threads_returns_total():
    with mock.patch("orchestrate.get_unresolved_threads_by_author", return_value=({"coderabbitai": 2}, 5)):
        assert orchestrate.get_unresolved_review_threads("bastion", 1) == 5


def test_get_pr_id_and_automerge_parsing():
    payload = json.dumps({"data": {"repository": {"pullRequest": {
        "id": "PR_1", "autoMergeRequest": {"enabledAt": "2026"}}}}})
    with mock.patch("orchestrate.run_gh", return_value=payload):
        pr_id, automerge = orchestrate.get_pr_id_and_automerge("bastion", 1)
    assert pr_id == "PR_1" and automerge is True


def test_get_pr_id_and_automerge_none_and_bad():
    with mock.patch("orchestrate.run_gh", return_value=None):
        assert orchestrate.get_pr_id_and_automerge("bastion", 1) == (None, False)
    with mock.patch("orchestrate.run_gh", return_value="{bad"):
        assert orchestrate.get_pr_id_and_automerge("bastion", 1) == (None, False)


def test_enable_auto_merge_success_and_failure():
    with mock.patch("orchestrate.run_gh", return_value="{}"):
        assert orchestrate.enable_auto_merge("bastion", "PR_1") is True
    with mock.patch("orchestrate.run_gh", return_value=None):
        assert orchestrate.enable_auto_merge("bastion", "PR_1") is False


# --------------------------------------------------------------------------
# process_pr decision tree
# --------------------------------------------------------------------------
def test_process_pr_details_none():
    with mock.patch("orchestrate.get_pr_details", return_value=None):
        assert orchestrate.process_pr("bastion", 1, _blank_state()) is False


def test_process_pr_cubic_retry_then_give_up():
    state = _blank_state()
    details = {"comments": [{"author": {"login": "cubic-dev-ai"}, "body": "cubic command failed"}]}
    with mock.patch("orchestrate.get_pr_details", return_value=details), \
         mock.patch("orchestrate.post_comment", return_value=True) as post:
        assert orchestrate.process_pr("bastion", 1, state) is True
        post.assert_called_once_with("bastion", 1, orchestrate.NUDGE_CUBIC_AUTOFIX)
    # bump to the cap and confirm it gives up (no nudge)
    state["prs"]["blixten85/bastion#1"]["cubic_retry_attempts"] = orchestrate.MAX_CUBIC_RETRY_ATTEMPTS
    with mock.patch("orchestrate.get_pr_details", return_value=details), \
         mock.patch("orchestrate.post_comment", return_value=True) as post:
        assert orchestrate.process_pr("bastion", 1, state) is False
        post.assert_not_called()


def test_process_pr_recently_attempted_skips():
    state = _blank_state()
    state["prs"]["blixten85/bastion#1"] = {"last_attempt": orchestrate.now_utc().isoformat()}
    details = {"comments": []}
    with mock.patch("orchestrate.get_pr_details", return_value=details):
        assert orchestrate.process_pr("bastion", 1, state) is False


def test_process_pr_rate_limited_enables_automerge():
    state = _blank_state()
    state["rate_limited_until"] = (orchestrate.now_utc() + timedelta(minutes=30)).isoformat()
    details = {"comments": []}
    with mock.patch("orchestrate.get_pr_details", return_value=details), \
         mock.patch("orchestrate.get_pr_id_and_automerge", return_value=("PR_1", False)), \
         mock.patch("orchestrate.enable_auto_merge", return_value=True) as enable:
        assert orchestrate.process_pr("bastion", 1, state) is False
        enable.assert_called_once()


def test_process_pr_merge_conflict_nudges_resolve():
    state = _blank_state()
    details = {"comments": [], "mergeable": "CONFLICTING"}
    with mock.patch("orchestrate.get_pr_details", return_value=details), \
         mock.patch("orchestrate.post_comment", return_value=True) as post:
        assert orchestrate.process_pr("bastion", 1, state) is True
        post.assert_called_once_with("bastion", 1, orchestrate.NUDGE_MERGE_CONFLICT)
    assert orchestrate.merge_conflict_attempts(state, "bastion", 1) == 1


def test_process_pr_merge_conflict_escalates_after_cap():
    state = _blank_state()
    state["prs"]["blixten85/bastion#1"] = {"merge_conflict_attempts": orchestrate.MAX_MERGE_CONFLICT_ATTEMPTS}
    details = {"comments": [], "mergeable": "CONFLICTING"}
    with mock.patch("orchestrate.get_pr_details", return_value=details), \
         mock.patch("orchestrate.escalate_to_claude", return_value=True) as esc:
        assert orchestrate.process_pr("bastion", 1, state) is False
        esc.assert_called_once()
    assert orchestrate.already_escalated(state, "bastion", 1) is True


def test_process_pr_behind_updates_branch():
    state = _blank_state()
    details = {"comments": [], "mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND"}
    with mock.patch("orchestrate.get_pr_details", return_value=details), \
         mock.patch("orchestrate.update_branch", return_value=True) as upd:
        assert orchestrate.process_pr("bastion", 1, state) is True
        upd.assert_called_once()
    assert state["nudges"][-1]["type"] == "update_branch"


def test_process_pr_needs_review_nudges_both_bots():
    state = _blank_state()
    details = {"comments": [], "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
               "statusCheckRollup": [], "reviews": []}
    with mock.patch("orchestrate.get_pr_details", return_value=details), \
         mock.patch("orchestrate.post_comment", return_value=True) as post:
        assert orchestrate.process_pr("bastion", 1, state) is True
    bodies = [c.args[2] for c in post.call_args_list]
    assert orchestrate.NUDGE_REVIEW in bodies
    assert orchestrate.NUDGE_SENTRY_REVIEW in bodies
    assert state["nudges"][-1]["type"] == "review"


def _reviewed_details():
    """A PR that already has CodeRabbit + Seer checks and a real review."""
    return {
        "comments": [{"author": {"login": "coderabbitai"}, "body": "x"}],
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"name": "CodeRabbit"}, {"name": "Seer Code Review"}],
        "reviews": [{"author": {"login": "coderabbitai"}, "body": "Reviewed"}],
    }


def test_process_pr_autofix_when_unresolved_threads():
    state = _blank_state()
    with mock.patch("orchestrate.get_pr_details", return_value=_reviewed_details()), \
         mock.patch("orchestrate.get_unresolved_threads_by_author",
                    return_value=({"coderabbitai": 1, "cubic-dev-ai": 1}, 2)), \
         mock.patch("orchestrate.post_comment", return_value=True) as post:
        assert orchestrate.process_pr("bastion", 1, state) is True
    bodies = [c.args[2] for c in post.call_args_list]
    assert orchestrate.NUDGE_AUTOFIX in bodies
    assert orchestrate.NUDGE_CUBIC_AUTOFIX in bodies
    assert orchestrate.autofix_attempts(state, "bastion", 1) == 1


def test_process_pr_resolve_fallback_after_autofix_exhausted():
    state = _blank_state()
    state["prs"]["blixten85/bastion#1"] = {"autofix_attempts": orchestrate.MAX_AUTOFIX_ATTEMPTS}
    with mock.patch("orchestrate.get_pr_details", return_value=_reviewed_details()), \
         mock.patch("orchestrate.get_unresolved_threads_by_author", return_value=({"coderabbitai": 1}, 1)), \
         mock.patch("orchestrate.post_comment", return_value=True) as post:
        assert orchestrate.process_pr("bastion", 1, state) is True
        post.assert_called_once_with("bastion", 1, orchestrate.NUDGE_RESOLVE)
    assert orchestrate.resolve_attempts(state, "bastion", 1) == 1


def test_process_pr_escalates_after_resolve_exhausted():
    state = _blank_state()
    state["prs"]["blixten85/bastion#1"] = {
        "autofix_attempts": orchestrate.MAX_AUTOFIX_ATTEMPTS,
        "resolve_attempts": orchestrate.MAX_RESOLVE_ATTEMPTS,
    }
    with mock.patch("orchestrate.get_pr_details", return_value=_reviewed_details()), \
         mock.patch("orchestrate.get_unresolved_threads_by_author", return_value=({"coderabbitai": 1}, 1)), \
         mock.patch("orchestrate.escalate_to_claude", return_value=True) as esc:
        assert orchestrate.process_pr("bastion", 1, state) is False
        esc.assert_called_once()
    assert orchestrate.already_escalated(state, "bastion", 1) is True


def test_process_pr_only_outdated_threads_falls_to_resolve():
    """Unresolved but non-actionable (outdated/other bots) skips autofix."""
    state = _blank_state()
    with mock.patch("orchestrate.get_pr_details", return_value=_reviewed_details()), \
         mock.patch("orchestrate.get_unresolved_threads_by_author", return_value=({}, 2)), \
         mock.patch("orchestrate.post_comment", return_value=True) as post:
        assert orchestrate.process_pr("bastion", 1, state) is True
        post.assert_called_once_with("bastion", 1, orchestrate.NUDGE_RESOLVE)


def test_process_pr_all_clear_enables_automerge():
    state = _blank_state()
    with mock.patch("orchestrate.get_pr_details", return_value=_reviewed_details()), \
         mock.patch("orchestrate.get_unresolved_threads_by_author", return_value=({}, 0)), \
         mock.patch("orchestrate.get_pr_id_and_automerge", return_value=("PR_1", False)), \
         mock.patch("orchestrate.enable_auto_merge", return_value=True) as enable:
        assert orchestrate.process_pr("bastion", 1, state) is False
        enable.assert_called_once()


def test_process_pr_all_clear_automerge_already_on():
    state = _blank_state()
    with mock.patch("orchestrate.get_pr_details", return_value=_reviewed_details()), \
         mock.patch("orchestrate.get_unresolved_threads_by_author", return_value=({}, 0)), \
         mock.patch("orchestrate.get_pr_id_and_automerge", return_value=("PR_1", True)), \
         mock.patch("orchestrate.enable_auto_merge", return_value=True) as enable:
        assert orchestrate.process_pr("bastion", 1, state) is False
        enable.assert_not_called()


# --------------------------------------------------------------------------
# process_pr — post/action failure branches (no quota consumed)
# --------------------------------------------------------------------------
def test_process_pr_cubic_retry_post_fails():
    state = _blank_state()
    details = {"comments": [{"author": {"login": "cubic-dev-ai"}, "body": "cubic command failed"}]}
    with mock.patch("orchestrate.get_pr_details", return_value=details), \
         mock.patch("orchestrate.post_comment", return_value=False):
        assert orchestrate.process_pr("bastion", 1, state) is False
    assert state["nudges"] == []


def test_process_pr_merge_conflict_post_fails():
    state = _blank_state()
    details = {"comments": [], "mergeable": "CONFLICTING"}
    with mock.patch("orchestrate.get_pr_details", return_value=details), \
         mock.patch("orchestrate.post_comment", return_value=False):
        assert orchestrate.process_pr("bastion", 1, state) is False
    assert state["nudges"] == []


def test_process_pr_merge_conflict_already_escalated_short_circuits():
    state = _blank_state()
    state["prs"]["blixten85/bastion#1"] = {
        "merge_conflict_attempts": orchestrate.MAX_MERGE_CONFLICT_ATTEMPTS,
        "escalated_to_claude": True,
    }
    details = {"comments": [], "mergeable": "CONFLICTING"}
    with mock.patch("orchestrate.get_pr_details", return_value=details), \
         mock.patch("orchestrate.escalate_to_claude") as esc:
        assert orchestrate.process_pr("bastion", 1, state) is False
        esc.assert_not_called()


def test_process_pr_behind_update_fails():
    state = _blank_state()
    details = {"comments": [], "mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND"}
    with mock.patch("orchestrate.get_pr_details", return_value=details), \
         mock.patch("orchestrate.update_branch", return_value=False):
        assert orchestrate.process_pr("bastion", 1, state) is False
    assert state["nudges"] == []


def test_process_pr_review_post_fails():
    state = _blank_state()
    details = {"comments": [], "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
               "statusCheckRollup": [], "reviews": []}
    with mock.patch("orchestrate.get_pr_details", return_value=details), \
         mock.patch("orchestrate.post_comment", return_value=False):
        assert orchestrate.process_pr("bastion", 1, state) is False
    assert state["nudges"] == []


def test_process_pr_autofix_post_fails():
    state = _blank_state()
    with mock.patch("orchestrate.get_pr_details", return_value=_reviewed_details()), \
         mock.patch("orchestrate.get_unresolved_threads_by_author", return_value=({"coderabbitai": 1}, 1)), \
         mock.patch("orchestrate.post_comment", return_value=False):
        assert orchestrate.process_pr("bastion", 1, state) is False
    assert state["nudges"] == []


def test_process_pr_autofix_with_other_unactionable_threads():
    """Actionable CodeRabbit thread plus extra non-actionable threads (line 644)."""
    state = _blank_state()
    with mock.patch("orchestrate.get_pr_details", return_value=_reviewed_details()), \
         mock.patch("orchestrate.get_unresolved_threads_by_author", return_value=({"coderabbitai": 1}, 3)), \
         mock.patch("orchestrate.post_comment", return_value=True) as post:
        assert orchestrate.process_pr("bastion", 1, state) is True
        post.assert_called_once_with("bastion", 1, orchestrate.NUDGE_AUTOFIX)


def test_process_pr_resolve_post_fails():
    state = _blank_state()
    state["prs"]["blixten85/bastion#1"] = {"autofix_attempts": orchestrate.MAX_AUTOFIX_ATTEMPTS}
    with mock.patch("orchestrate.get_pr_details", return_value=_reviewed_details()), \
         mock.patch("orchestrate.get_unresolved_threads_by_author", return_value=({"coderabbitai": 1}, 1)), \
         mock.patch("orchestrate.post_comment", return_value=False):
        assert orchestrate.process_pr("bastion", 1, state) is False
    assert state["nudges"] == []


def test_process_pr_resolve_exhausted_already_escalated_short_circuits():
    state = _blank_state()
    state["prs"]["blixten85/bastion#1"] = {
        "autofix_attempts": orchestrate.MAX_AUTOFIX_ATTEMPTS,
        "resolve_attempts": orchestrate.MAX_RESOLVE_ATTEMPTS,
        "escalated_to_claude": True,
    }
    with mock.patch("orchestrate.get_pr_details", return_value=_reviewed_details()), \
         mock.patch("orchestrate.get_unresolved_threads_by_author", return_value=({"coderabbitai": 1}, 1)), \
         mock.patch("orchestrate.escalate_to_claude") as esc:
        assert orchestrate.process_pr("bastion", 1, state) is False
        esc.assert_not_called()


# --------------------------------------------------------------------------
# main() orchestration loop
# --------------------------------------------------------------------------
def test_main_stops_when_quota_exhausted(in_state_dir, monkeypatch):
    monkeypatch.setattr(orchestrate, "REPOS", ["bastion"])
    state = _blank_state()
    now = orchestrate.now_utc().isoformat()
    state["nudges"] = [{"ts": now, "type": "review"}] * orchestrate.QUOTA_PER_HOUR
    with mock.patch("orchestrate.load_state", return_value=state), \
         mock.patch("orchestrate.list_open_prs") as list_prs, \
         mock.patch("orchestrate.save_state") as save:
        orchestrate.main()
        list_prs.assert_not_called()
        save.assert_called_once()


def test_main_processes_prs(in_state_dir, monkeypatch):
    monkeypatch.setattr(orchestrate, "REPOS", ["bastion"])
    with mock.patch("orchestrate.load_state", return_value=_blank_state()), \
         mock.patch("orchestrate.list_open_prs", return_value=[1, 2]), \
         mock.patch("orchestrate.process_pr", return_value=False) as proc, \
         mock.patch("orchestrate.save_state"):
        orchestrate.main()
        assert proc.call_count == 2


def test_main_no_open_prs(in_state_dir, monkeypatch):
    monkeypatch.setattr(orchestrate, "REPOS", ["bastion"])
    with mock.patch("orchestrate.load_state", return_value=_blank_state()), \
         mock.patch("orchestrate.list_open_prs", return_value=[]), \
         mock.patch("orchestrate.process_pr") as proc, \
         mock.patch("orchestrate.save_state"):
        orchestrate.main()
        proc.assert_not_called()


def test_main_stops_mid_repo_when_quota_hits_zero(in_state_dir, monkeypatch):
    monkeypatch.setattr(orchestrate, "REPOS", ["bastion"])

    def consume(repo, number, state):
        state["nudges"].append({"ts": orchestrate.now_utc().isoformat(), "type": "review"})
        return True

    state = _blank_state()
    with mock.patch("orchestrate.load_state", return_value=state), \
         mock.patch("orchestrate.list_open_prs", return_value=list(range(10))), \
         mock.patch("orchestrate.process_pr", side_effect=consume) as proc, \
         mock.patch("orchestrate.save_state") as save:
        orchestrate.main()
    # Should stop once QUOTA_PER_HOUR nudges are consumed.
    assert proc.call_count == orchestrate.QUOTA_PER_HOUR
    save.assert_called_once()
