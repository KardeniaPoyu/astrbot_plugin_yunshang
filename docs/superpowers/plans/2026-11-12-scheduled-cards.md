# Scheduled Campus Cards Implementation Plan

> **For agentic workers:** Use subagent-driven-development or executing-plans to implement these bounded tasks. Check off validation as it completes.

**Goal:** Every four hours deliver attractive, grounded campus-post cards to configured groups without repeating successful deliveries.

**Architecture:** The existing read-only API client supplies a time-bounded batch. A pure digest layer prepares AI summaries or excerpts; Pillow renders cards; an SQLite ledger and one async scheduler handle independent per-group/page retries.

**Tech Stack:** Python 3.11+, httpx, Pillow, SQLite, AstrBot plugin hooks.

**Spec:** ../specs/2026-11-12-scheduled-cards-design.md

## Global Constraints

- Fixed Asia/Shanghai four-hour windows; defaults disabled and targets empty in public configuration.
- At most 10 API pages of 20 posts, 8 displayed items, 4 items per card.
- No credentials, deployment target IDs, captured traffic or personal posts in source control.
- No extra LLM calls on ordinary chat. Summarization happens once per prepared batch; failure uses excerpts.

## Review Focus

- Old pinned posts and duplicate IDs: client window tests.
- Missing/future timestamps and page limits: client must report incomplete coverage.
- Reload while sending / failure after page one: delivery lease and per-page ledger tests.
- Bad AI JSON / injected post / invalid cited ID: digest tests and deterministic fallback.
- Chinese punctuation, long tokens, missing font and long content: card layout tests plus visual inspection.

## Task 1 — Repository baseline

- [x] Initialize isolated plugin repository; commit only plugin source, tests, documentation.
- [x] Add repository URL, installation documentation and credential exclusions; inspect staged files before public push.

## Task 2 — Time-window collection

Files: client.py, tests/test_client.py.

Interface: `CampusClient.fetch_window(start, end, *, max_pages=10, page_size=20) -> dict`, with aware datetimes and result keys `posts`, `total`, `scanned`, `scan_limited`, `invalid_dates`.

- [x] Add tests for pinned/duplicate posts, multiple pages, empty windows, date boundaries and mid-fetch authentication failure.
- [x] Collect and deduplicate posts in `[start, end)`, retain at most2000 characters of sanitized body per post; recheck school after collection.
- [x] Run client tests.

## Task 3 — Card renderer

Files: cards.py, tests/test_cards.py, examples/render_preview.py, assets/preview.png.

Interface: `render_digest(digest:dict, output_dir, font_path='') -> list[str]`.

Digest keys: `community`, `window_start`, `window_end`, `total`, `shown`, `scan_limited`, `mode` (`ai`/`excerpt`), `intro`, `items`. Items contain `id`, `title`, `summary`, `published_at`, `category`, `image_count`.

- [x] Implement local CJK-font renderer, bounded wrapping, pagination and explicit missing-font failure.
- [x] Test mixed Chinese/Latin, long content, missing fonts and multi-page output.
- [x] Render synthetic fixture preview and inspect it; no real user posts in examples.

## Task 4 — Digest and delivery

Files: digest.py, delivery.py, tests/test_digest.py, tests/test_delivery.py.

- [x] Build deterministic excerpt digest, accept only AI items referencing input IDs and bounded text.
- [x] Persist prepared batches and `(window,target,page)` outcomes in SQLite; claim with leases, retry only failures with bounded backoff.
- [x] Test first-run baseline, most-recent-window catchup, retries, partial page success, empty batches and concurrency.

## Task 5 — AstrBot integration and deployment

Files: main.py, _conf_schema.json, requirements.txt, metadata.yaml, README.md.

- [x] Add one cancellable scheduler, configured provider selection, thread-offloaded rendering, admin preview/status commands and local state directory.
- [x] Validate full suite, render a server-side sample, verify enabled schedule/targets without sending test messages.
- [x] Publish clean source to the standalone GitHub repository and deploy v0.2.0; preserve private Cookie configuration.
