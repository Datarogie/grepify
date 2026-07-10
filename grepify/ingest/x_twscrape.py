"""GRP-50: twscrape-backed :class:`~grepify.ingest.x.TweetSource` + CI
account/session management.

This is the only module that imports twscrape, and it does so **lazily** (the
optional ``x`` extra - ``uv sync --extra x``): the core package and ``make
check`` never require it, matching X's best-effort status (PRD §13). Tests never
reach here - they drive :class:`~grepify.ingest.x.XFetcher` with
:class:`~grepify.ingest.x.FakeTweetSource` - so this stays a thin adapter whose
job is (1) twscrape account/session management from CI secrets and (2) mapping
every twscrape failure onto :class:`~grepify.errors.FetchError` so X stays
isolated (PRD §9). twscrape's API is async; this wraps it behind a synchronous
:class:`~grepify.ingest.x.TweetSource` so the fetcher contract is unchanged.

Account/session management (CI, PRD §5 security)
------------------------------------------------
Burner accounts come from CI secrets, never the repo. :func:`accounts_from_env`
reads them; nothing here ever logs a credential, cookie, or session header
(PRD §5 - rely on GitHub's secret masking, don't build a parallel one).

- **``GREPIFY_X_ACCOUNTS``** (required to run live): a JSON array of burner
  accounts, each ``{"username","password","email","email_password"}`` - the
  argument shape twscrape's ``pool.add_account`` takes. A ``"cookies"`` string
  may be supplied instead of the password pair for a pre-authed session.
- **``GREPIFY_X_ACCOUNTS_DB``** (optional): path to twscrape's accounts SQLite
  db, so a logged-in session is cached between runs (via the CI cache) instead
  of re-authing every run. Absent -> an ephemeral in-memory pool each run.

If ``GREPIFY_X_ACCOUNTS`` is unset, :func:`build_tweet_source` returns ``None``
and the ``ingest`` CLI constructs ``XFetcher(None, ...)`` - every X source then
degrades to a logged skip (see :mod:`grepify.ingest.x`). **Live-account wiring
is pending Kyle adding the ``GREPIFY_X_ACCOUNTS`` secret**; until then this path
is exercised only by its fixtures/fakes, and ``sources/groups/x-watchlist.yml``
ships ``enabled: false``.

Failure modes
-------------
- twscrape not installed -> :func:`build_tweet_source` raises
  :class:`~grepify.errors.FetchError` (a systemic-looking message, but the CLI
  treats a missing extra as "unconfigured" and passes ``None`` instead, so it is
  still a skip). Malformed ``GREPIFY_X_ACCOUNTS`` JSON -> :class:`ValueError`
  at startup (a deploy misconfiguration, surfaced loudly, not silently ignored).
- Any twscrape error while fetching (login challenge, rate limit, suspended /
  locked account, empty pool) -> :class:`~grepify.errors.FetchError` labelled by
  :func:`~grepify.ingest.x.classify_x_failure`; the orchestrator isolates it.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from collections.abc import Sequence
from typing import Any

from grepify.clock import to_iso
from grepify.errors import FetchError
from grepify.ingest.x import Tweet, TweetSource, classify_x_failure

_ACCOUNTS_ENV = "GREPIFY_X_ACCOUNTS"
_ACCOUNTS_DB_ENV = "GREPIFY_X_ACCOUNTS_DB"
_ACCOUNT_FIELDS = ("username", "password", "email", "email_password")


def accounts_from_env(env: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Parse ``GREPIFY_X_ACCOUNTS`` into a list of twscrape account dicts.

    Returns ``[]`` when the secret is unset (nothing to run). Raises
    :class:`ValueError` for malformed JSON or an entry missing both a password
    and a cookies string - a deploy misconfiguration surfaced loudly rather than
    a silent no-op (PRD §10 - no silent behavior changes).
    """
    source = env if env is not None else dict(os.environ)
    raw = source.get(_ACCOUNTS_ENV)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{_ACCOUNTS_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"{_ACCOUNTS_ENV} must be a JSON array of account objects")
    accounts: list[dict[str, str]] = []
    for index, entry in enumerate(parsed):
        if not isinstance(entry, dict) or "username" not in entry:
            raise ValueError(f"{_ACCOUNTS_ENV}[{index}] must be an object with a username")
        if "password" not in entry and "cookies" not in entry:
            raise ValueError(
                f"{_ACCOUNTS_ENV}[{index}] must carry either a password (with email/"
                "email_password) or a cookies string"
            )
        accounts.append({k: str(v) for k, v in entry.items()})
    return accounts


def build_tweet_source(env: dict[str, str] | None = None) -> TweetSource | None:
    """Build the live :class:`TweetSource` from CI secrets, or ``None`` if X is
    not configured (no ``GREPIFY_X_ACCOUNTS``) - the CLI then passes ``None`` to
    :class:`~grepify.ingest.x.XFetcher` so X degrades to a logged skip.
    """
    source = env if env is not None else dict(os.environ)
    accounts = accounts_from_env(source)
    if not accounts:
        return None
    return TwscrapeTweetSource(accounts, accounts_db=source.get(_ACCOUNTS_DB_ENV) or None)


class TwscrapeTweetSource:
    """Live :class:`TweetSource` backed by twscrape (see module docstring).

    Constructed with already-parsed account dicts + an optional accounts-db
    path; imports twscrape lazily so the optional extra is only needed when this
    is actually built. The account pool is (re)logged-in on first use.
    """

    def __init__(
        self, accounts: Sequence[dict[str, str]], *, accounts_db: str | None = None
    ) -> None:
        self._accounts = list(accounts)
        self._accounts_db = accounts_db
        self._api: Any | None = None

    def tweets(self, handle: str, *, since_id: str | None, limit: int) -> list[Tweet]:
        try:
            return asyncio.run(self._tweets_async(handle, since_id=since_id, limit=limit))
        except FetchError:
            raise
        except Exception as exc:  # map EVERY twscrape/transport
            # fault to a labelled FetchError so X stays isolated (PRD §9/§13).
            label = classify_x_failure(exc)
            raise FetchError(f"{handle}: x {label}") from exc

    async def _tweets_async(self, handle: str, *, since_id: str | None, limit: int) -> list[Tweet]:
        api = await self._ensure_api()
        user = await api.user_by_login(handle)
        if user is None:
            raise FetchError(f"{handle}: x account not found or unavailable")
        collected: list[Tweet] = []
        async for tweet in api.user_tweets(user.id, limit=limit):
            mapped = _map_twscrape_tweet(tweet)
            if since_id is not None and int(mapped.id) <= int(since_id):
                continue
            collected.append(mapped)
        return collected

    async def _ensure_api(self) -> Any:
        if self._api is not None:
            return self._api
        twscrape = _import_twscrape()
        api = twscrape.API(self._accounts_db) if self._accounts_db else twscrape.API()
        for account in self._accounts:
            await api.pool.add_account(
                account["username"],
                account.get("password", ""),
                account.get("email", ""),
                account.get("email_password", ""),
                cookies=account.get("cookies"),
            )
        await api.pool.login_all()
        self._api = api
        return api


def _import_twscrape() -> Any:
    try:
        return importlib.import_module("twscrape")
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise FetchError(
            "twscrape is not installed - run `uv sync --extra x` to enable the X fetcher"
        ) from exc


def _map_twscrape_tweet(tweet: Any) -> Tweet:
    """Map a twscrape ``Tweet`` object onto our neutral :class:`Tweet`.

    Kept tiny and defensive: twscrape's object carries far more than the PRD §6
    schema has a home for (metrics, media, ...) - only id/url/text/author/date/
    lang are read; everything else is dropped (see :mod:`grepify.ingest.x`).
    """
    created = getattr(tweet, "date", None)
    user = getattr(tweet, "user", None)
    return Tweet(
        id=str(tweet.id),
        url=str(getattr(tweet, "url", "")),
        text=str(getattr(tweet, "rawContent", None) or getattr(tweet, "content", "") or ""),
        author=getattr(user, "username", None) if user is not None else None,
        created_at=to_iso(created) if created is not None else None,
        lang=getattr(tweet, "lang", None),
    )
