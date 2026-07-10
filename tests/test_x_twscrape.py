"""GRP-50: twscrape account/session management from CI secrets.

Covers only the parts that are testable without twscrape or a network: parsing
``GREPIFY_X_ACCOUNTS`` and deciding whether X is configured. The live fetch path
(login, session, tweet fetch) is a thin lazy-import adapter exercised through
:class:`grepify.ingest.x.FakeTweetSource` in ``test_x_fetcher`` instead.
"""

from __future__ import annotations

import json

import pytest

from grepify.ingest.x_twscrape import (
    TwscrapeTweetSource,
    accounts_from_env,
    build_tweet_source,
)

_ACCOUNT = {
    "username": "burner1",
    "password": "pw",
    "email": "b1@example.com",
    "email_password": "epw",
}


def test_no_secret_means_no_accounts() -> None:
    assert accounts_from_env({}) == []


def test_parses_account_array() -> None:
    env = {"GREPIFY_X_ACCOUNTS": json.dumps([_ACCOUNT])}
    accounts = accounts_from_env(env)
    assert accounts == [_ACCOUNT]


def test_cookies_may_replace_password() -> None:
    entry = {"username": "burner2", "cookies": "auth_token=...; ct0=..."}
    accounts = accounts_from_env({"GREPIFY_X_ACCOUNTS": json.dumps([entry])})
    assert accounts[0]["cookies"].startswith("auth_token=")


def test_malformed_json_raises() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        accounts_from_env({"GREPIFY_X_ACCOUNTS": "{not json"})


def test_non_array_raises() -> None:
    with pytest.raises(ValueError, match="must be a JSON array"):
        accounts_from_env({"GREPIFY_X_ACCOUNTS": json.dumps({"username": "x"})})


def test_missing_username_raises() -> None:
    with pytest.raises(ValueError, match="username"):
        accounts_from_env({"GREPIFY_X_ACCOUNTS": json.dumps([{"password": "pw"}])})


def test_missing_password_and_cookies_raises() -> None:
    with pytest.raises(ValueError, match="password"):
        accounts_from_env({"GREPIFY_X_ACCOUNTS": json.dumps([{"username": "x"}])})


def test_build_tweet_source_none_without_accounts() -> None:
    assert build_tweet_source({}) is None


def test_build_tweet_source_returns_live_source_with_accounts() -> None:
    env = {"GREPIFY_X_ACCOUNTS": json.dumps([_ACCOUNT]), "GREPIFY_X_ACCOUNTS_DB": "/tmp/acc.db"}
    source = build_tweet_source(env)
    # Constructed but not logged-in: twscrape is only imported on first fetch, so
    # this holds even without the optional `x` extra installed.
    assert isinstance(source, TwscrapeTweetSource)
