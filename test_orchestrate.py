#!/usr/bin/env python3
"""Tester för bot-identitets-hjälparna i orchestrate.py.

Fokuserar på trust-boundaryn som PR:en härdar: att en login verifieras exakt
(inte via substräng) och att GitHub Apps "[bot]"-suffix normaliseras
konsekvent, både i is_bot_author och i grupperingen av olösta trådar per bot.
"""
import json
import unittest
from unittest import mock

import orchestrate
from orchestrate import (
    CODERABBIT_LOGINS,
    CUBIC_LOGINS,
    get_unresolved_threads_by_author,
    is_bot_author,
    normalize_login,
)


def _graphql_threads_response(threads):
    """Bygg ett GraphQL-svar (som en JSON-sträng, precis som run_gh returnerar)
    med en enda sida av reviewThreads. Varje tråd anges som
    (author_login, is_resolved, is_outdated)."""
    nodes = []
    for author_login, is_resolved, is_outdated in threads:
        nodes.append({
            "isResolved": is_resolved,
            "isOutdated": is_outdated,
            "comments": {"nodes": [{"author": {"login": author_login}}]},
        })
    return json.dumps({
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }
    })


class NormalizeLoginTest(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(normalize_login("CodeRabbitAI"), "coderabbitai")

    def test_strips_bot_suffix(self):
        self.assertEqual(normalize_login("coderabbitai[bot]"), "coderabbitai")

    def test_strips_bot_suffix_case_insensitive(self):
        self.assertEqual(normalize_login("CodeRabbitAI[BOT]"), "coderabbitai")

    def test_none_becomes_empty(self):
        self.assertEqual(normalize_login(None), "")

    def test_leaves_plain_login_untouched(self):
        self.assertEqual(normalize_login("cubic-dev-ai"), "cubic-dev-ai")


class IsBotAuthorTest(unittest.TestCase):
    def test_exact_coderabbit_login_matches(self):
        self.assertTrue(is_bot_author("coderabbitai", CODERABBIT_LOGINS))

    def test_coderabbit_bot_suffix_matches(self):
        self.assertTrue(is_bot_author("coderabbitai[bot]", CODERABBIT_LOGINS))

    def test_exact_cubic_login_matches(self):
        self.assertTrue(is_bot_author("cubic-dev-ai", CUBIC_LOGINS))

    def test_cubic_bot_suffix_matches(self):
        self.assertTrue(is_bot_author("cubic-dev-ai[bot]", CUBIC_LOGINS))

    def test_substring_impostor_rejected(self):
        # Kärnan i säkerhetsfixen: en login som bara *innehåller* "coderabbit"
        # får INTE passera.
        self.assertFalse(is_bot_author("coderabbit-x", CODERABBIT_LOGINS))
        self.assertFalse(is_bot_author("notcubic", CUBIC_LOGINS))

    def test_none_and_empty_rejected(self):
        self.assertFalse(is_bot_author(None, CODERABBIT_LOGINS))
        self.assertFalse(is_bot_author("", CODERABBIT_LOGINS))

    def test_wrong_allowlist_rejected(self):
        self.assertFalse(is_bot_author("coderabbitai", CUBIC_LOGINS))


class UnresolvedThreadsByAuthorTest(unittest.TestCase):
    def _run_with_threads(self, threads):
        response = _graphql_threads_response(threads)
        with mock.patch.object(orchestrate, "run_gh", return_value=response):
            return get_unresolved_threads_by_author("some-repo", 1)

    def test_groups_plain_logins(self):
        by_author, total = self._run_with_threads([
            ("coderabbitai", False, False),
            ("cubic-dev-ai", False, False),
        ])
        self.assertEqual(by_author, {"coderabbitai": 1, "cubic-dev-ai": 1})
        self.assertEqual(total, 2)

    def test_strips_bot_suffix_when_grouping(self):
        # Kärnan i konsekvens-fixen: en "[bot]"-suffixad login normaliseras
        # till samma nyckel som process_pr slår upp ("coderabbitai"), så
        # autofix-nudgen inte tyst faller tillbaka till @resolve.
        by_author, total = self._run_with_threads([
            ("coderabbitai[bot]", False, False),
            ("cubic-dev-ai[bot]", False, False),
        ])
        self.assertEqual(by_author, {"coderabbitai": 1, "cubic-dev-ai": 1})
        self.assertEqual(total, 2)

    def test_outdated_counts_in_total_but_not_by_author(self):
        by_author, total = self._run_with_threads([
            ("coderabbitai[bot]", False, True),
        ])
        self.assertEqual(by_author, {})
        self.assertEqual(total, 1)

    def test_resolved_threads_ignored(self):
        by_author, total = self._run_with_threads([
            ("coderabbitai", True, False),
        ])
        self.assertEqual(by_author, {})
        self.assertEqual(total, 0)


if __name__ == "__main__":
    unittest.main()
