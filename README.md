# admentum-mcp

A read-only [MCP](https://modelcontextprotocol.io) server that syncs your messages from [Admentum](https://admentum.se), the Swedish school platform, to local storage, and serves them to a personal AI agent one item at a time.

The server does the mechanical work: it logs in as you, downloads new messages and attachments, turns PDFs and Word files into text, and tracks which items the agent has handled. Your agent does the understanding: it summarizes, picks out dates and to-dos, and writes the digest.

> **Status: testing and verification.** Syncing, storage, attachment extraction and all MCP tools work and are tested against a live account. Open checks are listed in `documentation/SPECIFICATION.md`, section 11.

Unofficial and not affiliated with Admentum. The connector uses the same internal calls as the Admentum web app, which can change without notice.

## What to know before using it

- **Your password stays local.** It's read from environment variables, held in memory, and never logged or written to disk.
- **Messages stay local.** Everything is stored under `~/.admentum-mcp` with owner-only permissions. Logs contain only IDs and counts, never content.
- **Syncing marks conversations as read in Admentum.** Admentum marks a conversation as read when it is fetched, and there is no way around that. Treat your agent's digest as your inbox.
- **Attachments with text become text; images need a vision model.** PDFs with a text layer, Word files and plain text are converted to text the agent can read and search. Photos, posters and scanned letters are not run through OCR: the agent looks at them with `get_attachment_image`, so use a model that can see images. The original files are always kept.
- **Only recent history is fetched.** On the first sync, conversations older than `retention_days` (default 365) are skipped entirely.
- **Linux only.** WSL2 works if the repo and data stay on the Linux filesystem.

## Setup

Requires [uv](https://docs.astral.sh/uv/). It installs Python 3.12 and the dependencies.

```sh
git clone <this repo> && cd admentum-mcp
uv sync
cp .env.sample .env && chmod 600 .env   # then fill in your Admentum login
```

`.env` is only read in development. In production, pass `ADMENTUM_USERNAME` and `ADMENTUM_PASSWORD` as environment variables from your MCP client's server configuration.

Settings live in `~/.admentum-mcp/config.toml`, created on first start with every setting listed and commented out at its default. Uncomment a line to change it, then restart the server. `admentum-mcp config` prints the full template, including settings added in later versions.

## Usage

```sh
uv run admentum-mcp sync --dry-run   # log in and report what would be downloaded
uv run admentum-mcp sync             # download new messages and attachments
uv run admentum-mcp status           # counts, failures, unsupported file types
uv run admentum-mcp prune            # list what retention would delete (add --apply to delete)
uv run admentum-mcp config           # print every setting with its default
uv run admentum-mcp --source fake sync   # try it without an Admentum account
```

## Connecting an agent

The server speaks MCP over stdio. Register it in your MCP client with a command like this (the exact format depends on the client):

```json
{
  "command": "uv",
  "args": ["run", "--project", "/path/to/admentum-mcp", "admentum-mcp", "serve"],
  "env": { "ADMENTUM_USERNAME": "...", "ADMENTUM_PASSWORD": "..." }
}
```

Tools: `sync`, `list_pending`, `get_item`, `mark_seen`, `search` and `status`, plus `get_attachment_text`, `get_attachment_image` (photos, posters and scanned pages, for vision-capable agents), `grep_item`, `list_threads`, `get_thread_context` and a maintenance `prune`. A typical run: `sync`, `list_pending`, then `get_item` and `mark_seen` per item; `search` answers questions about earlier messages. Message text is wrapped in `<untrusted_school_content>` tags so the agent treats it as data, not instructions, and results are kept compact to save tokens.

Give the agent only the MCP tools, not file access to `~/.admentum-mcp`; it keeps its own memory of what it learns.

For a scheduled digest and a long-term "school profile" (children, classes, teachers, routines, dates and to-dos), see the example skill in [`examples/school-inbox/SKILL.md`](examples/school-inbox/SKILL.md). It is an example to adapt to your agent, not part of the server.

To try it without an account, add `--source fake` before `serve`.

## Development

```sh
uv run pytest                          # unit tests; no network
uv run scripts/probe_admentum.py       # read-only live probe; prints field shapes, never content
```

`documentation/SPECIFICATION.md` is the design: rules (`R-…`), the data model, the tools, the verified Admentum protocol, and the verification status.

## License

MIT
