"""Live, read-only probe of the Admentum protocol (spec section 10).

Makes only GET requests besides login and token. Prints field names, types, counts, sizes,
cookie names and domains, status codes and IDs. Never prints message content, names,
file names, cookie values, tokens or credentials.

Conversations marked unread are not opened unless --allow-mark-read is given, because
opening one may mark it read in Admentum (question 4).

Usage: uv run scripts/probe_admentum.py [--config PATH] [--max-threads N] [--snapshot PATH] [--allow-mark-read]
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from admentum_mcp.admentum import MESSAGES, AdmentumSource
from admentum_mcp.config import credentials_from_env, load_config, load_dotenv
from admentum_mcp.source import SchoolSourceError

FILES = "https://files.admentum.se"


def out(*a: Any) -> None:
    print(*a, flush=True)


def section(title: str) -> None:
    out(f"\n== {title}")


def where(url: httpx.URL | str) -> str:
    """Host and path only; query strings may carry codes or tokens."""
    u = urlsplit(str(url))
    return f"{u.netloc}{u.path}"


def shape(value: Any, path: str, acc: dict[str, Counter[str]]) -> None:
    """Collect path -> type counts. Values are never recorded."""
    if isinstance(value, dict):
        acc[path or "<root>"]["object"] += 1
        for k, v in value.items():
            shape(v, f"{path}.{k}" if path else k, acc)
    elif isinstance(value, list):
        acc[path]["list"] += 1
        acc[path + "[]len"][f"len={min(len(value), 10)}{'+' if len(value) >= 10 else ''}"] += 1
        for v in value:
            shape(v, path + "[]", acc)
    else:
        acc[path][type(value).__name__] += 1


def print_shape(acc: dict[str, Counter[str]]) -> None:
    for p in sorted(acc):
        out(f"  {p}: " + ", ".join(f"{t} x{n}" for t, n in sorted(acc[p].items())))


def fmt_pattern(s: Any) -> str:
    """The format of a timestamp with digits masked, e.g. dddd-dd-ddTdd:dd:dd.dddddd+dd:dd."""
    return re.sub(r"\d", "d", s) if isinstance(s, str) else type(s).__name__


def jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


class Probe:
    """Raw access on top of the connector's session (login, token, cookies)."""

    def __init__(self, src: AdmentumSource) -> None:
        self.src = src
        self.c = src._client

    @property
    def user_id(self) -> str:
        return self.src._user_id or ""

    @property
    def token(self) -> str:
        return self.src._token or ""

    def cookie_summary(self) -> None:
        for ck in self.c.cookies.jar:
            out(f"  cookie {ck.name}: domain={ck.domain} path={ck.path} secure={ck.secure} "
                f"expires={'session' if ck.expires is None else int(ck.expires - time.time())}s")

    def api(self, path: str) -> httpx.Response:
        return self.c.get(f"{MESSAGES}/{self.user_id}/{path}", headers={"User-Info": self.token})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path)
    ap.add_argument("--max-threads", type=int, default=8)
    ap.add_argument("--snapshot", type=Path, help="write conversation and message IDs here (for question 1)")
    ap.add_argument("--allow-mark-read", action="store_true", help="also open one unread conversation (question 4)")
    args = ap.parse_args()

    load_dotenv(Path.cwd() / ".env")
    cfg = load_config(args.config)
    creds = credentials_from_env()
    src = AdmentumSource(cfg, creds)
    p = Probe(src)
    section("Login and token (Q5, Q8)")
    try:
        src._ensure_token()
    except SchoolSourceError as e:
        out(f"\nLOGIN FAILED: {type(e).__name__}")
        return 1
    out(f"  user_id discovered: {bool(p.user_id)}")
    p.cookie_summary()
    token = p.token
    claims = jwt_claims(token)
    out(f"  JWT claim names: {sorted(claims)}; sub is numeric: {str(claims.get('sub', '')).isdigit()}")
    if "exp" in claims and "iat" in claims:
        out(f"  token lifetime: {int(claims['exp']) - int(claims['iat'])} s")
    out(f"  claims equal to user_id: {[k for k, v in claims.items() if str(v) == p.user_id]}")

    section("Conversation list (Q2)")
    convs: list[dict[str, Any]] = []
    page = 1
    while True:
        r = p.api(f"conversations?per_page=100&page={page}")
        out(f"  page {page}: {r.status_code} {r.headers.get('content-type', '')}")
        if r.status_code != 200:
            return 1
        body = r.json()
        if page == 1:
            acc: dict[str, Counter[str]] = defaultdict(Counter)
            shape(body, "", acc)
            out("  shape of page 1:")
            print_shape(acc)
        batch = body.get("conversations") or []
        convs.extend(batch)
        if body.get("has_next_page") != (len(batch) == 100):
            out(f"  page {page}: has_next_page={body.get('has_next_page')} with {len(batch)} entries")
        if len(batch) < 100:
            break
        page += 1
    out(f"  conversations: {len(convs)}")
    out(f"  with latest_message_at: {sum(1 for c in convs if c.get('latest_message_at'))}")
    out(f"  with latest_message.created_at: {sum(1 for c in convs if (c.get('latest_message') or {}).get('created_at'))}")
    out(f"  unread: {sum(1 for c in convs if c.get('is_unread'))}")
    count_keys = sorted({k for c in convs for k in c if re.search(r"count|total|num", k, re.I)})
    out(f"  count-like keys: {count_keys}")
    out(f"  latest_message_at formats: {sorted({fmt_pattern(c.get('latest_message_at')) for c in convs})}")
    uid = p.user_id
    out(f"  conversations created by configured user_id: {sum(1 for c in convs if str((c.get('creator') or {}).get('id')) == uid)}")
    out(f"  conversations listing user_id among recipients: "
        f"{sum(1 for c in convs if any(str(r.get('id')) == uid for r in c.get('recipients') or []))}")
    out(f"  flag values: {dict(Counter(c.get('flag') for c in convs))}")
    out(f"  latest_message.message_type values: {dict(Counter((c.get('latest_message') or {}).get('message_type') for c in convs))}")
    out(f"  latest_message with meta_id: {sum(1 for c in convs if (c.get('latest_message') or {}).get('meta_id'))}")
    out(f"  is_information values: {dict(Counter((c.get('json_recipients') or {}).get('is_information') for c in convs))}")
    years = Counter(((c.get('latest_message') or {}).get('created_at') or '')[:4] for c in convs)
    out(f"  latest activity by year: {dict(sorted(years.items()))}")
    ids = [c.get("id") for c in convs]
    out(f"  id types: {sorted({type(i).__name__ for i in ids})}; unique: {len(set(map(str, ids))) == len(ids)}")

    section("Conversations (Q3, Q6, Q7, Q9, Q10)")
    read = [c for c in convs if not c.get("is_unread")]
    chosen = read[: args.max_threads]
    if args.allow_mark_read:
        unread = [c for c in convs if c.get("is_unread")]
        if unread:
            chosen.append(unread[0])
    msg_acc: dict[str, Counter[str]] = defaultdict(Counter)
    conv_acc: dict[str, Counter[str]] = defaultdict(Counter)
    stats: Counter[str] = Counter()
    created_formats: set[str] = set()
    meta_ids: list[str] = []
    snapshot: dict[str, list[str]] = {}
    pages_seen = Counter()
    for c in chosen:
        msgs: list[dict[str, Any]] = []
        page = 1
        while True:
            r = p.api(f"conversations/{c['id']}?page={page}&per_page=50")
            if r.status_code != 200:
                out(f"  conversation {c['id']}: {r.status_code}")
                stats["fetch_errors"] += 1
                break
            conv = r.json().get("conversation") or {}
            if page == 1:
                shape({k: v for k, v in conv.items() if k != "messages"}, "conversation", conv_acc)
            batch = conv.get("messages") or []
            msgs.extend(batch)
            if len(batch) < 50:
                break
            page += 1
        pages_seen[page] += 1
        lm = c.get("latest_message") or {}
        newest = max(msgs, key=lambda m: m.get("created_at") or "", default=None)
        if newest is not None:
            stats["list_latest_id_matches_newest"] += lm.get("id") == newest.get("id")
            stats["list_latest_content_equal_full"] += lm.get("content") == newest.get("content")
            stats["list_latest_meta_id_equal"] += lm.get("meta_id") == newest.get("meta_id")
            stats["list_latest_created_at_equal"] += lm.get("created_at") == newest.get("created_at")
            stats["threads_with_messages"] += 1
        stats[f"threads_with_{min(len(msgs), 5)}{'+' if len(msgs) >= 5 else ''}_messages"] += 1
        snapshot[str(c["id"])] = [str(m.get("id")) for m in msgs]
        for m in msgs:
            shape(m, "message", msg_acc)
            stats["messages"] += 1
            created_formats.add(fmt_pattern(m.get("created_at")))
            stats[f"message_type={m.get('message_type')}"] += 1
            creator = m.get("creator") or {}
            if str(creator.get("id")) == p.user_id:
                stats["creator_is_user_id"] += 1
            if m.get("meta_id"):
                stats["with_meta_id"] += 1
                meta_ids.append(str(m["meta_id"]))
            content = m.get("content") or ""
            if "<blockquote" in content.lower():
                stats["body_has_blockquote"] += 1
            if re.search(r"(^|\n|>)\s*&gt;|(^|\n)\s*>", content):
                stats["body_has_gt_quote"] += 1
            if re.search(r"(skrev|wrote)\s*:", content, re.I):
                stats["body_has_reply_header"] += 1
        if c is not chosen[-1]:
            time.sleep(0.2)
        if args.allow_mark_read and c.get("is_unread"):
            time.sleep(1)
            r = p.api("conversations?per_page=100&page=1")
            after = next((x for x in r.json().get("conversations") or [] if x.get("id") == c["id"]), None)
            out(f"  unread conversation {c['id']} still unread after opening: {after and after.get('is_unread')}")
    out(f"  opened {len(chosen)} conversations; pages per conversation: {dict(pages_seen)}")
    out("  conversation fields:")
    print_shape(conv_acc)
    out("  message fields:")
    print_shape(msg_acc)
    out(f"  stats: {dict(stats)}")
    out(f"  created_at formats: {sorted(created_formats)}")

    if args.snapshot:
        prev = json.loads(args.snapshot.read_text()) if args.snapshot.exists() else None
        if prev:
            same = {k: prev[k] == v for k, v in snapshot.items() if k in prev}
            out(f"  snapshot comparison (Q1): {sum(same.values())}/{len(same)} conversations have identical message IDs")
        args.snapshot.write_text(json.dumps(snapshot))
        args.snapshot.chmod(0o600)

    section("Files (Q3, Q8)")
    if meta_ids:
        mid = meta_ids[0]
        r = p.c.get(f"{FILES}/meta", params={"uuid": mid})
        out(f"  meta: {r.status_code} {r.headers.get('content-type', '')}")
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            acc = defaultdict(Counter)
            shape(r.json(), "", acc)
            print_shape(acc)
        with p.c.stream("GET", f"{FILES}/get", params={"uuid": mid}) as r:
            size = sum(len(chunk) for chunk in r.iter_bytes())
            out(f"  get: {r.status_code} {r.headers.get('content-type', '')} {size} bytes "
                f"via {[where(h.url) for h in r.history]} final {where(r.url)}")
        out(f"  meta_id format: {sorted({fmt_pattern(re.sub('[a-f]', 'x', m)) for m in meta_ids[:20]})}")
    else:
        out("  no attachments in the opened conversations")

    section("Token renewal (Q5)")
    src._renew_token()
    r = p.api("conversations?per_page=1&page=1")
    out(f"  renewed token differs: {p.token != token}; works: {r.status_code == 200}")
    p.cookie_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
