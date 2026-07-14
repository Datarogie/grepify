"""SQLite cache schema (DDL) - the derived query cache of PRD §6.

This is the v1 cache backend only. It is rebuilt from JSONL truth every run and
never committed. The *logical* column set (PRD §6) is the contract both backends
implement; Postgres (v2) will express the same columns with its own DDL. SQL is
lowercase and there is no ``select *`` anywhere (repo style).

Failure modes
-------------
Executed inside :class:`~grepify.repository.jsonl_sqlite.JsonlSqliteRepository`;
a malformed statement would surface as ``sqlite3.OperationalError`` wrapped in
:class:`~grepify.errors.RepositoryError` during rebuild.
"""

from __future__ import annotations

# Booleans are stored as integer (0/1) per PRD §6. Timestamps are text (ISO-8601).
SCHEMA_DDL = """
create table sources (
  source_id      text primary key,
  name           text not null,
  kind           text not null check (kind in ('rss','youtube','reddit','x')),
  url            text not null,
  url_hash       text not null unique,
  group_id       text not null,
  enabled        integer not null default 1,
  added_at       text not null,
  config_json    text
);

create table source_groups (
  group_id       text primary key,
  name           text not null,
  category       text not null,
  enabled        integer not null default 1,
  builtin        integer not null default 0
);

create table items (
  item_id        text primary key,
  source_id      text not null,
  kind           text not null,
  external_id    text,
  canonical_url  text not null,
  title          text not null,
  summary        text,
  author         text,
  published_at   text not null,
  fetched_at     text not null,
  content_hash   text not null,
  transcript_ref text,
  lang           text
);
create index idx_items_published on items(published_at);
create index idx_items_source on items(source_id, published_at);
create unique index idx_items_dedup on items(kind, external_id);

create table item_keywords (
  item_id        text not null,
  keyword        text not null,
  rank           integer not null,
  method         text not null,
  model          text,
  extracted_at   text not null,
  primary key (item_id, keyword, method)
);
create index idx_kw_keyword on item_keywords(keyword);

-- v2-reserved: deliberately unpopulated in v1. Aliases live in keywords.yml and
-- are applied at trend-query time (PRD §6 note, grepify.keywords); nothing
-- writes this table until a v2 backend needs it queryable rather than YAML-only.
create table keyword_aliases (
  alias          text primary key,
  canonical      text not null
);

create table digests (
  digest_id      text primary key,
  kind           text not null check (kind in ('daily','weekly')),
  category       text not null,
  period_start   text not null,
  period_end     text not null,
  title          text not null,
  body_md        text not null,
  top_keywords   text not null,
  model          text not null,
  prompt_version text not null,
  created_at     text not null
);

create table fetch_log (
  source_id      text not null,
  run_id         text not null,
  started_at     text not null,
  status         text not null check (status in ('ok','empty','error','skipped')),
  items_new      integer not null default 0,
  error          text,
  duration_ms    integer,
  rung           text
);

create table llm_log (
  run_id         text not null,
  purpose        text not null,
  model          text not null,
  input_items    integer,
  tokens_in      integer,
  tokens_out     integer,
  status         text not null,
  created_at     text not null
);
"""
