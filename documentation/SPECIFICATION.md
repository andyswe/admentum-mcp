# admentum-mcp – Specification

A Python stdio MCP server that syncs Admentum conversations, every message in them and their attachments into local storage, converts them to text, tracks per-message state, and serves a personal agent (for example Hermes) through a small, read-only tool set. Only the server is in scope; agent-side setup is section 8. Rule IDs (`R-…`) are referenced by tests and code comments.

**Maintaining this document.** Keep it updated and aligned with the current status in `AGENTS.md`. As the project moves forward it can be compacted: it holds no history or reasoning, only what the current and future status need.

## 1. Principles

- **R-P1 Division of labor.** The server does the mechanical work: fetching, extraction, HTML to text, truncation, state, pruning. It applies no heuristics about wording or language (no quote stripping, period detection or built-in classification rules), because those break silently on real messages. The agent does the understanding and keeps the extracted knowledge; the server never stores events or tasks.
- **R-P2 Small context.** The agent gets a short manifest, then one item at a time.
- **R-P3 Read-only toward the school.** No tool sends messages or changes the Admentum account. One accepted side effect: Admentum marks a conversation read when it is fetched (section 10).
- **R-P4 Untrusted content.** Message and attachment text in tool results is wrapped in untrusted-content markers (6.1). Subject, sender, group and file names are untrusted too, and every untrusted string has marker tags neutralized so it cannot open or close a marker.
- **R-P5 Idempotent.** Running `sync` any number of times never duplicates anything.
- **R-P6 Privacy.** Children's data. Logs hold IDs, counts and sizes, never content, names or credentials. Data stays local; files `0o600`, directories `0o700`.
- **R-P7 Linux only** (WSL2 works). Repo and data dir on a Linux filesystem, not `/mnt/c`, where SQLite WAL and file locks are unreliable. `pathlib` throughout, no shell-outs.
- **R-P8 stdout is the MCP channel.** Logging goes to a rotating log file (and stderr from the CLI).

## 2. Stack and layout

- Python 3.12 (`.python-version`), uv, `pyproject.toml` + `uv.lock`.
- **R-S1** On startup the server checks that every required module is importable (including MarkItDown's PDF and DOCX backends) and exits naming any missing package.
- Libraries: `mcp` 2.x (`MCPServer`, stdio), `httpx`, `markitdown[pdf,docx]`, `pillow` and `pypdfium2` (images, R-V3), `beautifulsoup4`, `filelock`, `tzdata`; `sqlite3` and `tomllib` from the standard library; `pytest`.

```
src/admentum_mcp/
  __main__.py      # CLI (7.2)
  server.py        # MCP tools; thin layer over sync/store
  config.py        # config.toml + env loading and validation
  store.py         # SQLite schema, version check, queries
  files.py         # data dir layout, atomic writes, safe names, permissions
  sync.py          # sync algorithm (5.2)
  clean.py         # HTML to text
  extract.py       # attachment text (5.3)
  images.py        # images and PDF pages for the agent (5.4)
  prune.py
  source.py        # SchoolSource protocol, data classes, errors
  admentum.py      # the only Admentum-specific module (9)
  fake_source.py   # scriptable in-memory source for tests and `--source fake`
scripts/probe_admentum.py   # live read-only probe (10)
examples/school-inbox/SKILL.md   # example agent skill (8)
tests/
```

## 3. Storage

```
<data_dir>/                  # default ~/.admentum-mcp
  config.toml
  state.db                   # SQLite, WAL
  sync.lock
  logs/admentum-mcp.log      # rotating
  data/
    .tmp/                    # staging; emptied on startup and before each sync
    thread_<thread_id>/
      thread.json            # raw thread summary as last received
      items/<item_id>/
        item.json            # raw message (HTML body included)
        body.txt             # body as text
        attachments/<safe_name>, <safe_name>.txt   # original file, extracted text
```

- **R-F1** Directories are named by ID, never by position.
- **R-F2 Atomic item writes.** An item, attachments included, is written to `data/.tmp/<random>/` and moved into place with `os.replace`; its row is committed only after the move. A crash leaves nothing or a complete item; a leftover directory without a row is replaced.
- **R-F3 Safe names.** The original file name stays in the database. The on-disk name keeps Unicode letters, replaces `/`, `\`, NUL and control characters, strips leading dots and trailing dots and spaces (so `.` and `..` never survive), limits the length, and adds ` (2)` on a collision.
- **R-F4** Text files are UTF-8, NFC-normalized.
- **R-F5** Timestamps are stored in UTC as fixed-width `YYYY-MM-DDTHH:MM:SS.ffffffZ` (string order = time order) and shown in the configured `timezone`.

## 4. Data model

A **thread** is an Admentum conversation; an **item** is one message in it (position 0 is the root). Kind: `conversation` (default; replies may need history) or `feed` (content arrives as replies, such as one bulletin per week; each reply stands alone).

- **R-D1** The item is the unit of tracking; a thread is pending while any item is unseen.
- **R-D2 Identity** is the Admentum message ID; without one, `"sha-" + sha256(thread_id, sender, sent_at, body[:200])`. IDs that are not plain tokens are hashed for directory names.
- **R-D3 Order** is by sent time, never download time; positions are renumbered whenever a thread is fetched.
- **R-D4 Kind** comes only from config: `feed` if the thread ID is in `feeds.thread_ids` (wins) or it matches a `feeds.rules` entry (subject and/or root sender regex). No built-in rule.
- **R-D5 Own messages** are stored, seen at download, never pending.
- **R-D6 Item states:** `downloaded_at`, `seen_at` (by `mark_seen`, own message or noise rule; `seen_by` says which), `pruned_at` (files deleted, row kept).
- **R-D7 Edits and deletions at the source** are flagged once (`source_edited_at` when the body hash changes, `source_deleted_at` when the item disappears; cleared if it comes back). The local copy is never overwritten.
- **R-D9 Recipient groups.** `threads.groups` holds the group names the conversation was sent to (a class such as `3C`, an after-school group), as the source reports them, refreshed on every sync for every listed thread and shown as `to`. Individual recipient lists are never stored or shown (they name other families). The agent, not the server, maps groups to children.

**R-S2 Schema and no automatic migrations.** `PRAGMA user_version` holds the schema version (2). A new database is created in the current schema; a database with another version is refused with an error and never changed. A schema change bumps the version and is migrated by hand (or `reset --yes`).

```
threads(id TEXT PK, subject, kind NOT NULL, groups /* JSON list */, last_activity_at /* source value, stored only after a full fetch */,
        reply_count, last_checked_at, last_error, last_error_at, archived_at)
items(id TEXT PK, thread_id NOT NULL REFERENCES threads, position NOT NULL, received_at NOT NULL, sender, sender_id,
      is_mine NOT NULL DEFAULT 0, body_sha256, body_chars, status NOT NULL /* ok|partial|error */, error,
      downloaded_at, seen_at, seen_by /* agent|own|noise_rule */, pruned_at, source_edited_at, source_deleted_at,
      path /* relative to data/ */)
attachments(item_id NOT NULL REFERENCES items, source_ref NOT NULL /* meta_id */, filename, safe_name, mime, size_bytes,
            sha256, duplicate_of /* "item_id/source_ref" */, text_chars, truncated NOT NULL DEFAULT 0,
            extract_status NOT NULL /* ok|failed|unsupported|download_failed */, error, attempts NOT NULL DEFAULT 0,
            PRIMARY KEY (item_id, source_ref))
sync_runs(id INTEGER PK, started_at, finished_at, result /* ok|partial|error|dry_run */, counts_json, error)
meta(key TEXT PK, value)   -- first_sync_at, which anchors the ingest cutoff
```

## 5. Behavior

### 5.1 Source interface (`source.py`)

```python
class SchoolSource(Protocol):
    def list_threads(self) -> list[ThreadSummary]: ...  # id, title, last_activity_at, reply_count, unread, raw, groups
    def get_thread(self, thread_id: str) -> list[Message]: ...  # all pages; id|None, sent_at (aware), sender_id, sender_name, is_mine, html, attachment_refs, raw
    def download_attachment(self, ref: str, dest_dir: Path, max_bytes: int) -> AttachmentFile: ...  # filename, mime, size, path
```

The source returns raw HTML and decides `is_mine`. Errors are typed, never empty results, all `SchoolSourceError`: `AuthError` (login rejected), `SessionError` (renewal failed), `SourceError` (network or HTTP after retries; `AttachmentTooLarge` over `max_bytes`), `ProtocolError` (unexpected shape).

### 5.2 `sync`

- **R-Y1** Takes `sync.lock` without waiting; if held, returns `already_running`.
- **R-Y2 Order of work:**
  1. Retry `failed` attachments locally and `download_failed` ones by downloading again.
  2. `list_threads()`: insert unknown threads, update subject, kind and groups, set or clear `archived_at` for threads missing from or back in the list. Threads whose latest activity is before the ingest cutoff (first sync time minus `sync.initial_lookback_days`) are not fetched; unknown ones are not stored at all, known ones are recorded as checked.
  3. A thread is changed when new, never fetched successfully, in error, or when its source `last_activity_at` or `reply_count` differs from the stored value. Archived threads are skipped. Without either field, threads with an item within `sync.full_recheck_days` are polled every sync, older ones every `sync.stale_recheck_every` syncs.
  4. Per changed thread, `get_thread()`; per message: older than the cutoff → skip; known → R-D7 checks; new → stage, download and extract attachments, convert the body, commit (R-F2), then auto-seen (R-D5, R-Y6). Mark known items missing from the source (R-D7) and renumber.
  5. Wait `sync.thread_delay_ms` between threads; auto-prune (5.5); record a `sync_runs` row.
- **R-Y3 Failure isolation.** A failed thread fetch records `last_error`, keeps `last_activity_at`, and is retried next sync; other threads continue. A failed attachment makes the item `partial` (R-X4).
- **R-Y4** `AuthError` or `SessionError` aborts the sync with `result: error`; never reported as "no new messages".
- **R-Y5 Result** (counts only): `new_threads`, `new_items_in_new_threads`, `new_replies`, `changed_threads`, `auto_seen`, `edited`, `deleted_at_source`, `attachment_failures`, `unsupported_attachments`, `thread_errors`, `pruned_items`, `pending_total`, `result` (`ok`, `partial` if a thread failed, `error` + `error`, `dry_run`, or `already_running` alone).
- **R-Y6 Noise rules** (optional): items matching a `noise.rules` entry are seen with `seen_by = noise_rule`.
- **R-Y7 Dry run** (CLI only) lists and fetches changed threads and reports what would be downloaded (`attachments_to_download`); it writes no thread, item or file, only its `sync_runs` row. Fetching still marks conversations read.
- **R-Y8 Startup:** empty `data/.tmp/`, run R-S1, re-extract `failed` attachments and `unsupported` ones whose type now has an extractor.

### 5.3 Text and attachment extraction

- **R-C1** HTML becomes text with paragraphs, lists and line breaks kept and entities decoded. The body is stored whole: Admentum does not quote earlier messages (10), and quote heuristics cut real content.
- **R-X1 Text types:** PDF and DOCX through MarkItDown's converters called directly (its dispatcher falls back to reading a broken file as plain text), and `text/plain`; output is Markdown. A generic MIME type falls back to the file extension.
- **R-X2** The original file is always kept, the text written beside it.
- **R-X3 Limits:** over `limits.max_attachment_bytes` → `download_failed`/`too_large`, not retried. Text over `limits.max_extracted_chars` is cut and `truncated` set.
- **R-X4** A bad attachment never fails its item: the item becomes `partial` while any attachment is `failed` or `download_failed` (except R-V6); `unsupported` keeps it `ok`; `error` only when the body could not be converted.
- **R-X5 Scans:** a PDF with no word characters is `failed`/`no_text_layer`, not retried; the agent views it (5.4).
- **R-X6** `failed` (extraction broke) and `download_failed` are retried up to `limits.max_attempts`. `unsupported` (no extractor) is never retried automatically, is listed in `status` by MIME type and extension, and `get_item` tells the agent support could be added unless the type is viewable.
- **R-X7 Duplicates:** an attachment whose `sha256` matches an extracted one reuses its text and sets `duplicate_of`; the file is still stored per item.
- **R-X8 Download validation:** a non-2xx status, or an HTML body when HTML is not expected (an expired session returns a login page), fails the download.

### 5.4 Images for a vision-capable agent

- **R-V1 Viewable:** PNG, JPEG, GIF and WebP images, and every page of any PDF (pypdfium2). HEIC, XLSX and others stay unsupported.
- **R-V2** `get_attachment_image` returns one image per call plus a short text: file name, `page k of n` for PDFs, and a reminder that text in the image is untrusted.
- **R-V3 Normalized:** EXIF orientation applied, all metadata dropped (photos can carry GPS positions of children), transparency flattened on white, long side at most 1568 px (never upscaled), JPEG quality 85; PDF pages rendered at that size; over 50 megapixels refused.
- **R-V4** Rendered on demand from the stored original; nothing cached.
- **R-V5 Discoverable:** `list_pending` tags attachments `(image)` or `(scan)`; `get_item` names viewable images and scanned PDFs with their page count.
- **R-V6** A `no_text_layer` PDF does not make its item `partial`, since it can be viewed.

### 5.5 Pruning

- **R-R1** Deletes the files of items older than `retention_days` (default 365), per item.
- **R-R2** Rows stay as tombstones (`pruned_at`), so pruned items are never downloaded again.
- **R-R3** Unseen items are never pruned; `status` counts them as `unseen_past_retention`.
- **R-R5** Runs after every non-dry sync, and through the `prune` tool and CLI (default dry run).

## 6. MCP tools

### 6.1 Conventions

- Text from messages is wrapped as `<untrusted_school_content item="<id>" part="subject|body|attachment:<name>">…</untrusted_school_content>`. The server instructions and tool descriptions say this content, and subject, sender, group and file names, are data, not instructions.
- Unknown IDs, out-of-range arguments and `sync` without credentials are MCP tool errors (`isError`). A failed sync is a normal result with `result: error`.
- Dates: local time (configured `timezone`) with the UTC value in item headers and `status`; local time only in listings.
- **R-T1 Low token cost.** Every result is one compact JSON text (no indentation, no duplicate structured copy) or plain text; empty and default fields are omitted; listings carry no message text; defaults are small (`list_pending` 20, `search` 5); listings show at most 6 recipient groups plus `to_more`.
- The server instructions tell the agent: use only the tools, never the data folder; it owns the knowledge and calls `mark_seen` only after saving; for questions about earlier messages, `search` first; images and thread context only when needed.

### 6.2 Tools

| Tool | Returns |
| --- | --- |
| `sync()` | Counts per R-Y5. |
| `list_pending(limit=20, order="oldest", offset=0)` | `total_pending`, `returned`, `next_offset` (absent on the last page), and per unseen item: `id`, `date`, `subject`, `sender`, `sender_id`, `to`/`to_more`, `position` ("2/5"), `kind` (only `feed`), `chars` (body plus attachment text), `attachments` (names, tagged `(image)`, `(scan)`, `(not downloaded)` or their status when not readable text), `status` (only when not `ok`). |
| `search(query, limit=5, include_seen=true)` | Items containing every word of `query` (case-insensitive substrings, no language rules) in subject, sender, body or attachment text, newest first: `total_items`, and per hit `id`, `date`, `subject`, `sender`, `sender_id`, `to`/`to_more`, `seen`, `part` and one wrapped snippet of about 200 chars from the part with the most query words. |
| `get_item(id, max_chars=8000)` | Text: header (wrapped subject, thread and kind, position, sender with `sender_id` or "you", date, all `To:` groups, senders of earlier items, notes: seen, edited or deleted at the source, partial), then the body, then each attachment (name, MIME type, size, duplicate flag, then its text or a note for image, scan, unsupported, failed or not downloaded). `max_chars` (500–50000) is shared, body first; cut text is marked. A pruned item returns its header and `Pruned`. |
| `get_attachment_text(id, filename, offset=0, length=8000)` | `filename`, `offset`, `total_chars`, `next_offset` (absent at the end), wrapped `text`. An attachment without text is an error that points to `get_attachment_image` when viewable. |
| `get_attachment_image(id, filename, page=1)` | One image (R-V2, R-V3). Viewable attachments only (R-V1). |
| `grep_item(id, pattern, context_chars=80, max_matches=20)` | Case-insensitive literal matches (not a regex: an agent-chosen regex can backtrack without a timeout) in body and attachment text: `total_matches` and per match `part`, `offset`, wrapped context. |
| `list_threads(only_pending=true, limit=50)` | `total_threads` and per thread, most recent first: `thread_id`, `subject`, `kind`, `to`/`to_more`, `items`, `unseen`, `last_activity`, `archived`. |
| `get_thread_context(id, n=3, max_chars=8000)` | Text: the `n` (1–10) items before item `id`, oldest first, each body wrapped and cut to `max_chars / n`, with attachment names. |
| `mark_seen(ids)` | Marks 1–100 items seen (`seen_by = agent`) in one transaction; all or nothing, unknown IDs are named. `marked`, `already_seen`, `pending_total`. |
| `prune(older_than_days=None, dry_run=true)` | `dry_run`, `older_than_days`, `count`, `items` (at most 100 listed); applying takes the sync lock. Maintenance; not for the agent (8). |
| `status()` | Counts (threads, downloaded, pending, seen, partial and error items, pruned, `unseen_past_retention`, failed and not-downloaded attachments), unsupported types by MIME type and extension (with `viewable`), last sync, threads with errors, whether a sync is running, and `settings`: `retention_days`, `initial_lookback_days`, `fetched_since` (the date before which messages were never fetched; after the first sync), `timezone`, `max_attachment_mb`, and the number of `feed_rules` and `noise_rules`. Never `data_dir` (a local path) or rule patterns (they can hold names). |

## 7. Configuration and operation

### 7.1 Config and environment

`config.toml` (default `~/.admentum-mcp/config.toml`; `--config <path>` overrides). Every key has a default and nothing account-specific is configured (IDs are discovered at login, 9.1). Unknown keys and bad values fail startup naming the key.

- **R-S3 Config template.** On startup, if the file at the default path is missing, the server creates it (mode `0o600`) from the template in `config.py`: every setting with a one-line explanation and its default, **commented out**, so the file is a complete reference while defaults stay live (a default improved later still applies, and every uncommented line is the user's own choice). It is never overwritten. A missing `--config` file stays an error. `admentum-mcp config` prints the current template, so settings added in later versions can be found. A test keeps the template complete and equal to the built-in defaults.

The keys and defaults (the template adds an explanation per key):

```toml
data_dir = "~/.admentum-mcp"
retention_days = 365
timezone = "Europe/Stockholm"

[sync]
initial_lookback_days = 365   # default: retention_days
thread_delay_ms = 200
full_recheck_days = 30        # only when the source has no activity fields
stale_recheck_every = 10

[limits]
max_attachment_bytes = 104857600   # 100 MB
max_extracted_chars = 200000
max_attempts = 5

[feeds]
thread_ids = []
rules = []                    # e.g. [ { subject = "(?i)veckobrev", sender = "" } ]

[noise]
rules = []                    # same shape as feeds.rules
```

Credentials come only from `ADMENTUM_USERNAME` and `ADMENTUM_PASSWORD` in the environment, never from config, arguments, prompts or agent instructions, and are never logged. The CLI and the probe also load `.env` from the working directory (set variables win); `.env` is git-ignored and `.env.sample` lists the names. In production they come from `env` in the agent's server registration.

### 7.2 CLI

`admentum-mcp` (or `python -m admentum_mcp`), with `--config <path>` and `--source fake` (fixture scenario, no Admentum) on any command:

- `serve` (default): MCP over stdio; R-S1, R-S3, R-Y8; logs to the file only.
- `sync [--dry-run]`, `status`, `prune [--older-than-days N] [--apply]`: like the tools, JSON output.
- `reset --yes`: deletes `state.db` and `data/` under the sync lock; config and logs stay.
- `config`: prints the config template (every setting with its default); reads and creates nothing.

### 7.3 Operations

- SQLite in WAL mode; each item commit and state change is its own transaction.
- Network calls have timeouts and retry with exponential backoff (3 attempts) on connection errors, 429 and 5xx.
- Back up `state.db` with the data dir.

## 8. Agent integration

This repo delivers only the MCP server, used by a personal agent (for example [Hermes](https://hermes-agent.nousresearch.com/)) that the user configures. The agent's instructions, schedule and delivery are not part of this repo; keep the server usable by any MCP client.

- **Registration:** stdio, `command: uv`, `args: ["run", "--project", "<path>", "admentum-mcp", "serve"]`, credentials in `env` (7.1).
- **MCP only.** Never give the agent file access to `data_dir`: raw files lack the markers, bypass seen state and pruning, and hold what the tools leave out (recipient lists naming other families, image metadata).
- **Tools:** all except `prune`. `get_attachment_image` needs a model with image input.
- **Knowledge lives with the agent** (dates, to-dos, facts in its own memory); the server keeps messages and seen state. Questions about earlier messages: `search`, then `get_item` for the hits.
- **Assumed procedure:** `sync`, `list_pending`, then a few items per run: `get_item`, images or thread context only when needed, save the results, `mark_seen` (only after saving, so a crashed run shows the item again). Report urgent, then actions, then FYI, and say when items are left. Scheduling and delivery are the agent's; the server syncs only when `sync` is called.
- **Example:** `examples/school-inbox/SKILL.md` (Agent Skills format): backlog skimming, a few items per run oldest first, the digest, a school profile (children with birth year and grade, groups from `to`, people by `sender_id`, routines, calendar, events, to-dos, the yearly class rollover), and answering from the profile with `search`. An example to adapt, not part of the server's interface.

## 9. Admentum protocol (`admentum.py`)

The module mimics the web app's internal calls, which can change without notice, so everything Admentum-specific stays here. It logs in with username and password, as the web app does (Admentum's API tokens are normally not enabled for users). Verified against the live system in October 2026.

### 9.1 Session

One `httpx.Client` per sync with an in-memory cookie jar (cookies cross hosts by themselves; `sessionid` is set for `.admentum.se`) and timeouts on every request.

1. **Login page:** `GET https://skola.admentum.se/messages/` without a session redirects to `https://auth.admentum.se/?next=<NEXT>`, the app's OpenID request (`/openid/authorize?response_type=code&client_id=…`), so the client ID is never configured. CSRF token: `csrfmiddlewaretoken" value="([^"]+)"`. Token or `next` missing → `ProtocolError`.
2. **Credentials:** `POST` to that URL with `Referer` set to it, fields `csrfmiddlewaretoken`, `username`, `password`, `next`, following redirects. Never retried. The login form again, or no `sessionid` cookie → `AuthError`.
3. **Session:** `GET https://skola.admentum.se/messages/`. The page holds `current_user_id`, the user ID for the messages API (not in the JWT); missing → `ProtocolError`. `sessionid` lasts 14 days.
4. **Token:** `POST https://skola.admentum.se/user_token/` with `X-CSRFToken: <skola csrftoken>`, `Referer: …/messages/`, form body `csrfmiddlewaretoken=<csrftoken>` → `{"status": 200, "data": {"id_token", "access_token", "refresh_token"}}`. `id_token` is a JWT valid for 3600 s.
5. **Renewal:** repeat step 4 when the token is older than 45 minutes. On 401 or 403 from the messages API: renew and retry, then log in again and retry, then `SessionError`.

### 9.2 Messages API

Base `https://messages.admentum.se/api/users/<user_id>`, header `User-Info: <id_token>`.

- **List:** `GET conversations?per_page=100&page=N` → `{conversations, page, has_next_page, timestamp}`; stop when `has_next_page` is false. Entry: `id` (str), `title`, `is_unread`, `flag`, `creator`, `latest_message {id, created_at, content, meta_id, message_type}`, `recipients`, `json_recipients`. `last_activity_at = latest_message.created_at` (always present); no reply count.
- **Conversation:** `GET conversations/{id}?page=N&per_page=50` → `{conversation: {id, title, creator, is_unread, page, per_page, messages, recipients, json_recipients}}`; a short page is the last. `recipients` names every recipient (thousands for school-wide posts) and is never stored.
- **Message:** `id`, `created_at` (`YYYY-MM-DD HH:MM:SS.ffffff+HH:MM`, converted to UTC), `message_type` (1 text, 4 attachment), `creator {id, first_name, last_name}`, `content` (HTML), `meta_id` (`""` if none). Sender: `creator.name`, else first and last name, else `Unknown`; `sender_id = creator.id`; `is_mine = creator.id == user_id`. Each attachment is its own message with one `meta_id`.
- **Groups:** `json_recipients.names` maps group IDs to names per group type (`primary_groups`, `leisure_groups`); only these names are used (R-D9).

### 9.3 Files

Authenticated by cookies.

- **Metadata:** `GET https://files.admentum.se/meta?uuid=<meta_id>` → `{uuid, originalName, mimetype, size, …}`; defaults `meta_id` and `application/octet-stream`. A `size` over the limit → `AttachmentTooLarge` before downloading.
- **Download:** `GET https://files.admentum.se/get?uuid=<meta_id>`, streamed and cut off past the limit. No `Content-Type`, so the MIME type comes from the metadata. An HTML body when not expected triggers one fresh login and retry, then fails (R-X8).
- **Pacing:** at least 0.5 s between files-host requests; it rate-limits bursts with 403. Only a 401 or a login page served with 200 means an expired session; a 403 (rate limit) or 404 (deleted file) fails the attachment without a new login, and it is retried next sync.

### 9.4 Requirements from an earlier connector

A previous TypeScript connector (not included) lost data; each fix is a requirement here: fetch every changed thread in full inside the loop; keep state per thread and item instead of one last-run date (R-Y3); download only for new items or retries; raise typed errors instead of "no news" (R-Y4); keep the password in memory only; discover IDs instead of hardcoding them; keep Swedish characters in file names (R-F3); validate files-host responses (R-X8).

## 10. Probe findings

`scripts/probe_admentum.py` logs in with the env credentials, makes only GET calls besides login and token, and prints field names, types, counts, cookie names, status codes and IDs, never content. It skips unread conversations unless `--allow-mark-read` is given. Findings (October 2026):

1. **ID stability:** identical across runs on one day; re-check over days with `--snapshot` (11).
2. **Activity:** `latest_message.created_at` always present; no reply count.
3. **Attachments:** one per message, on separate `message_type` 4 messages.
4. **Read marking:** fetching a conversation marks it read. Accepted: the agent's digest is the inbox.
5. **Lifetimes:** `id_token` 3600 s, renewable from the session; `sessionid` 14 days.
6. **Quoted history:** none found, hence no quote stripping.
7. **Own messages:** `creator.id == user_id`.
8. **Cookies:** the jar works across hosts.
9. **Time format:** `YYYY-MM-DD HH:MM:SS.ffffff+HH:MM`.
10. **Recipients:** `json_recipients` holds group IDs per role and `names` (group ID → name), shown as `to`.
11. **Rate limiting:** the files host answers bursts with 403.

Accounts hold years of conversations; with the cutoff, a first sync opens only the recent ones.

## 11. Verification status

**Automated** (`uv run pytest`, no network): sync rules (bulletin replies over several syncs, idempotence, retries after a failed fetch, own messages, edits and deletions, out-of-order messages, crash safety, the lock, the cutoff, old threads with new replies, pruning, permissions), the connector against a mock Admentum (login failure, renewal and re-login, paging, typed errors, HTML instead of a file, rate-limit and deleted-file handling, group names), extraction (Swedish PDF and DOCX generated by `tests/docs.py`, scans, broken files, limits, duplicates, startup re-extraction), images (scaling, metadata and GPS removal, PDF pages), and every tool through an in-process MCP client plus a real stdio session that fails on stray stdout.

**Live** (October 2026): probe; dry run and full 365-day syncs on a real account; rows and files consistent, log free of content; real PDFs, Word files, images and scans; the tools through Claude Code as the agent.

**Open:**
- ID stability over days (probe `--snapshot`, two runs days apart).
- An agent-level run with the intended agent (Hermes): vision, the example skill, the school profile.
- Noise rules (R-Y6) are implemented and tested but unused so far.

**Decided against:** OCR and language detection (the agent views images and reads any language); config-based child tagging (replaced by recipient groups, R-D9); quote stripping and period labels (R-P1).
