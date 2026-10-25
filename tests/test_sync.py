"""Build step 1 acceptance tests (spec section 11) plus the sync rules they rely on."""

from __future__ import annotations

import re
import stat
from dataclasses import replace
from datetime import timedelta

import pytest
from filelock import FileLock

from conftest import START, Env
from admentum_mcp.config import FeedsConfig, Rule
from admentum_mcp.fake_source import FakeAttachment
from admentum_mcp.source import AuthError, SessionError


def pdf(name: str, data: bytes = b"%PDF-1.4 fake") -> FakeAttachment:
    return FakeAttachment(name, "application/pdf", data)


def txt(name: str, data: bytes) -> FakeAttachment:
    return FakeAttachment(name, "text/plain", data)


def test_bulletin_replies_arrive_over_three_syncs_each_pending_once(env: Env) -> None:
    env.cfg = replace(env.cfg, feeds=FeedsConfig(rules=(Rule(subject=re.compile("(?i)veckobrev")),)))
    src = env.source
    src.add_thread("100", "Veckobrev klass 3B")
    src.add_message("100", "r0", START - timedelta(days=60), "<p>Här kommer veckobreven.</p>")
    src.add_message("100", "w44", START - timedelta(hours=1), "<p>Vecka 44</p>",
                    attachments=[("m44", txt("Veckobrev v.44.txt", "Utflykt torsdag".encode()))])

    r1 = env.sync()
    assert r1["result"] == "ok"
    assert r1["new_threads"] == 1 and r1["new_items_in_new_threads"] == 2
    assert env.pending_ids() == ["r0", "w44"]
    (kind,) = env.query("SELECT kind FROM threads WHERE id = '100'")
    assert kind[0] == "feed"
    env.mark_all_seen()

    for week, ref in ((45, "m45"), (46, "m46")):
        env.clock.advance(days=7)
        src.add_message("100", f"w{week}", env.clock.now - timedelta(hours=1), f"<p>Vecka {week}</p>",
                        attachments=[(ref, txt(f"Veckobrev v.{week}.txt", f"Vecka {week}".encode()))])
        r = env.sync()
        assert r["new_replies"] == 1 and r["new_items_in_new_threads"] == 0
        assert r["pending_total"] == 1
        assert env.pending_ids() == [f"w{week}"]
        env.mark_all_seen()

    rows = env.query("SELECT id, position FROM items ORDER BY position")
    assert [(r["id"], r["position"]) for r in rows] == [("r0", 0), ("w44", 1), ("w45", 2), ("w46", 3)]
    assert (env.item_dir("w46") / "attachments" / "Veckobrev v.46.txt.txt").read_text() == "Vecka 46"


def test_sync_again_creates_nothing_new(env: Env) -> None:
    env.source.add_thread("1", "Hej")
    env.source.add_message("1", "a", START - timedelta(days=1), "<p>Hej</p>",
                           attachments=[("f1", pdf("a.pdf"))])
    env.sync()
    before = env.query("SELECT * FROM items")
    downloads = sum(1 for c in env.source.calls if c[0] == "download_attachment")

    env.clock.advance(hours=1)
    r = env.sync()
    assert all(r[k] == 0 for k in ("new_threads", "new_items_in_new_threads", "new_replies", "changed_threads"))
    assert [dict(x) for x in env.query("SELECT * FROM items")] == [dict(x) for x in before]
    assert env.source.fetches("1") == 1
    assert sum(1 for c in env.source.calls if c[0] == "download_attachment") == downloads


def test_failed_thread_fetch_is_retried_next_sync(env: Env) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "a1", START - timedelta(days=1), "x")
    env.source.add_thread("2", "B")
    env.source.add_message("2", "b1", START - timedelta(days=1), "y")
    env.sync()
    env.mark_all_seen()

    env.source.add_message("1", "a2", START, "new")
    env.source.fail_thread("1")
    env.clock.advance(hours=1)
    r = env.sync()
    assert r["result"] == "partial" and r["thread_errors"] == 1
    (t,) = env.query("SELECT last_error FROM threads WHERE id = '1'")
    assert t["last_error"]
    assert env.pending_ids() == []

    env.clock.advance(hours=1)
    r = env.sync()
    assert r["result"] == "ok" and r["new_replies"] == 1
    assert env.pending_ids() == ["a2"]
    (t,) = env.query("SELECT last_error FROM threads WHERE id = '1'")
    assert t["last_error"] is None
    assert env.source.fetches("2") == 1  # the healthy thread was not refetched


def test_own_messages_are_seen_automatically(env: Env) -> None:
    env.source.add_thread("1", "Fråga om utflykt")
    env.source.add_message("1", "t", START - timedelta(hours=3), "Fråga")
    env.source.add_message("1", "me", START - timedelta(hours=2), "Svar", is_mine=True, sender_id="parent-1")
    r = env.sync()
    assert r["auto_seen"] == 1
    assert env.pending_ids() == ["t"]
    (row,) = env.query("SELECT seen_by, is_mine FROM items WHERE id = 'me'")
    assert row["seen_by"] == "own" and row["is_mine"] == 1


def test_edit_and_deletion_at_source_flag_and_keep_local_copy(env: Env) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "a", START - timedelta(hours=3), "<p>Original</p>")
    env.source.add_message("1", "b", START - timedelta(hours=2), "<p>Second</p>")
    env.sync()

    env.source.edit_message("1", "a", "<p>Changed</p>")
    env.source.delete_message("1", "b")
    env.source.add_message("1", "c", START - timedelta(hours=1), "<p>Third</p>")  # bumps activity
    env.clock.advance(hours=1)
    r = env.sync()
    assert r["edited"] == 1 and r["deleted_at_source"] == 1
    rows = {x["id"]: x for x in env.query("SELECT * FROM items")}
    assert rows["a"]["source_edited_at"] and rows["b"]["source_deleted_at"]
    assert (env.item_dir("a") / "body.txt").read_text() == "Original"
    assert (env.item_dir("b") / "body.txt").read_text() == "Second"

    env.clock.advance(hours=1)
    env.source.add_message("1", "d", START, "<p>Fourth</p>")
    r = env.sync()
    assert r["edited"] == 0 and r["deleted_at_source"] == 0  # flagged once only


def test_out_of_order_message_gets_right_position(env: Env) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "first", START - timedelta(hours=5), "1")
    env.source.add_message("1", "third", START - timedelta(hours=1), "3")
    env.sync()
    env.source.add_message("1", "second", START - timedelta(hours=3), "2")
    env.source.add_message("1", "fourth", START - timedelta(minutes=30), "4")
    env.clock.advance(hours=1)
    env.sync()
    rows = env.query("SELECT id FROM items ORDER BY position")
    assert [r["id"] for r in rows] == ["first", "second", "third", "fourth"]


class Crash(BaseException):
    """Simulates the process dying; not caught by sync's error handling."""


def test_crash_in_middle_of_item_leaves_no_row_and_no_directory(env: Env, monkeypatch) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "a", START - timedelta(hours=1), "Hej",
                           attachments=[("f1", txt("one.txt", b"1")), ("f2", txt("two.txt", b"2"))])
    original = env.source.download_attachment

    def crashing(ref, dest_dir, max_bytes):
        if ref == "f2":
            raise Crash()
        return original(ref, dest_dir, max_bytes)

    monkeypatch.setattr(env.source, "download_attachment", crashing)
    with pytest.raises(Crash):
        env.sync()
    assert env.query("SELECT * FROM items") == []
    assert env.query("SELECT * FROM attachments") == []
    assert not (env.data.data / "thread_1" / "items" / "a").exists()

    monkeypatch.setattr(env.source, "download_attachment", original)
    r = env.sync()
    assert r["new_items_in_new_threads"] == 1
    assert sorted(p.name for p in (env.item_dir("a") / "attachments").iterdir()) == [
        "one.txt", "one.txt.txt", "two.txt", "two.txt.txt"]
    assert list(env.data.tmp.iterdir()) == []


def test_leftover_directory_without_row_is_replaced(env: Env) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "a", START - timedelta(hours=1), "Hej")
    leftover = env.data.data / "thread_1" / "items" / "a"
    leftover.mkdir(parents=True)
    (leftover / "junk").write_text("from a crash between move and commit")
    env.sync()
    assert sorted(p.name for p in leftover.iterdir()) == ["body.txt", "item.json"]


def test_second_sync_while_running_returns_already_running(env: Env) -> None:
    env.data.ensure()
    with FileLock(str(env.data.lock)):
        assert env.sync() == {"result": "already_running"}
    assert env.source.calls == []


def test_old_thread_with_new_reply_is_fetched(env: Env) -> None:
    env.source.add_thread("old", "Gammal tråd")
    env.source.add_message("old", "o1", START - timedelta(days=100), "gammalt")
    env.source.add_thread("quiet", "Tyst tråd")
    env.source.add_message("quiet", "q1", START - timedelta(days=100), "tyst")
    env.sync()
    env.mark_all_seen()

    for _ in range(3):
        env.clock.advance(days=1)
        env.sync()
    assert env.source.fetches("old") == 1

    env.source.add_message("old", "o2", env.clock.now, "nytt svar")
    env.clock.advance(hours=1)
    r = env.sync()
    assert r["new_replies"] == 1 and r["changed_threads"] == 1
    assert env.pending_ids() == ["o2"]
    assert env.source.fetches("quiet") == 1


def test_without_activity_fields_recent_threads_poll_and_stale_ones_poll_every_n(env: Env) -> None:
    env.source.report_activity = False
    env.source.add_thread("recent", "Ny")
    env.source.add_message("recent", "r1", START - timedelta(days=2), "x")
    env.source.add_thread("stale", "Gammal")
    env.source.add_message("stale", "s1", START - timedelta(days=90), "y")
    env.sync()  # run 0: both new
    for _ in range(10):
        env.clock.advance(hours=1)
        env.sync()  # runs 1..10; stale polls only when the previous-run count is a multiple of 10
    assert env.source.fetches("recent") == 11
    assert env.source.fetches("stale") == 2

    env.source.add_message("stale", "s2", env.clock.now, "nytt")
    for _ in range(10):
        env.clock.advance(hours=1)
        env.sync()
    assert "s2" in env.pending_ids()


def test_ingest_cutoff_is_respected(env: Env) -> None:
    lookback = env.cfg.sync.initial_lookback_days
    env.source.add_thread("1", "A")
    env.source.add_message("1", "too_old", START - timedelta(days=lookback + 1), "gammalt")
    env.source.add_message("1", "inside", START - timedelta(days=lookback - 1), "nyare")
    env.sync()
    assert [r["id"] for r in env.query("SELECT id FROM items")] == ["inside"]

    # The cutoff is anchored at the first sync, not moving with each sync.
    env.clock.advance(days=30)
    env.source.add_message("1", "late_but_after_cutoff", START - timedelta(days=lookback - 10), "z")
    env.source.add_message("1", "new", env.clock.now, "nytt")
    env.sync()
    ids = {r["id"] for r in env.query("SELECT id FROM items")}
    assert ids == {"inside", "late_but_after_cutoff", "new"}


def test_several_threads_are_each_fetched_in_full_in_one_run(env: Env) -> None:
    for t in "abc":
        env.source.add_thread(t, t)
        for i in range(3):
            env.source.add_message(t, f"{t}{i}", START - timedelta(hours=10 - i), f"{t}{i}")
    r = env.sync()
    assert r["new_threads"] == 3 and r["new_items_in_new_threads"] == 9 and r["changed_threads"] == 3


@pytest.mark.parametrize("error", [AuthError, SessionError])
def test_auth_failure_fails_the_sync(env: Env, error) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "a", START, "x", attachments=[("f", txt("a.txt", b"a"))])
    env.source.fail_attachment("f", error=error)
    r = env.sync()
    assert r["result"] == "error" and error.__name__ in r["error"]
    assert env.query("SELECT * FROM items") == []
    (run,) = env.query("SELECT result, error FROM sync_runs")
    assert run["result"] == "error" and run["error"]

    env.source.fail_login = True
    r = env.sync()
    assert r["result"] == "error" and "AuthError" in r["error"]


def test_failed_attachment_marks_item_partial_and_is_retried(env: Env) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "a", START, "x",
                           attachments=[("ok", txt("ok.txt", b"fine")), ("bad", txt("bad.txt", b"later"))])
    env.source.fail_attachment("bad")
    r = env.sync()
    assert r["attachment_failures"] == 1
    (item,) = env.query("SELECT status FROM items")
    assert item["status"] == "partial"

    env.clock.advance(hours=1)
    env.sync()
    (item,) = env.query("SELECT status FROM items")
    assert item["status"] == "ok"
    att = {a["source_ref"]: a for a in env.query("SELECT * FROM attachments")}
    assert att["bad"]["extract_status"] == "ok" and att["bad"]["attempts"] == 2
    assert (env.item_dir("a") / "attachments" / "bad.txt.txt").read_text() == "later"


def test_download_retries_stop_at_max_attempts_and_too_large_is_not_retried(env: Env) -> None:
    env.source.add_thread("1", "A")
    big = txt("big.txt", b"x" * (env.cfg.limits.max_attachment_bytes + 1))
    env.source.add_message("1", "a", START, "x", attachments=[("flaky", txt("f.txt", b"f")), ("big", big)])
    env.source.fail_attachment("flaky", times=100)
    for _ in range(8):
        env.sync()
        env.clock.advance(hours=1)
    att = {a["source_ref"]: a for a in env.query("SELECT * FROM attachments")}
    assert att["flaky"]["attempts"] == env.cfg.limits.max_attempts
    assert att["big"]["error"] == "too_large"
    downloads = [c[1] for c in env.source.calls if c[0] == "download_attachment"]
    assert downloads.count("big") == 1
    assert downloads.count("flaky") == env.cfg.limits.max_attempts


def test_duplicate_attachment_reuses_text(env: Env, monkeypatch) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "a", START - timedelta(hours=2), "x", attachments=[("f1", txt("a.txt", b"same"))])
    env.source.add_message("1", "b", START - timedelta(hours=1), "y", attachments=[("f2", txt("b.txt", b"same"))])
    calls = []
    import admentum_mcp.sync as sync_mod
    real = sync_mod.extract
    monkeypatch.setattr(sync_mod, "extract", lambda *a, **k: calls.append(a) or real(*a, **k))
    env.sync()
    assert len(calls) == 1
    (b,) = env.query("SELECT * FROM attachments WHERE source_ref = 'f2'")
    assert b["duplicate_of"] == "a/f1" and b["extract_status"] == "ok"
    assert (env.item_dir("b") / "attachments" / "b.txt").exists()
    assert (env.item_dir("b") / "attachments" / "b.txt.txt").read_text() == "same"


def test_unsupported_attachment_is_counted_and_item_stays_ok(env: Env) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "a", START, "x", attachments=[("img", FakeAttachment("bild.heic", "image/heic", b"\0"))])
    r = env.sync()
    assert r["unsupported_attachments"] == 1
    (item,) = env.query("SELECT status FROM items")
    assert item["status"] == "ok"


def test_swedish_and_colliding_attachment_names(env: Env) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "a", START, "x", attachments=[
        ("f1", txt("Läsläxa för åk 3.txt", b"1")),
        ("f2", txt("Läsläxa för åk 3.txt", b"2")),
        ("f3", txt("../../etc/passwd", b"3")),
    ])
    env.sync()
    rows = {a["source_ref"]: a for a in env.query("SELECT * FROM attachments")}
    assert rows["f1"]["safe_name"] == "Läsläxa för åk 3.txt"
    assert rows["f2"]["safe_name"] == "Läsläxa för åk 3 (2).txt"
    assert rows["f3"]["safe_name"] == "_.._etc_passwd"
    assert rows["f3"]["filename"] == "../../etc/passwd"


def test_permissions_are_private(env: Env) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "a", START, "x", attachments=[("f", txt("a.txt", b"a"))])
    env.sync()
    for p in [env.data.root, *env.data.root.rglob("*")]:
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == (0o700 if p.is_dir() else 0o600), p


def test_dry_run_writes_no_items_or_files(env: Env) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "a", START, "x", attachments=[("f", txt("a.txt", b"a"))])
    r = env.sync(dry_run=True)
    assert r["result"] == "dry_run"
    assert r["new_threads"] == 1 and r["new_items_in_new_threads"] == 1 and r["attachments_to_download"] == 1
    assert env.query("SELECT * FROM threads") == [] and env.query("SELECT * FROM items") == []
    assert not any(c[0] == "download_attachment" for c in env.source.calls)
    assert [p.name for p in env.data.data.iterdir() if p.name != ".tmp"] == []


def test_thread_missing_from_list_is_archived_and_restored(env: Env) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "a", START, "x")
    env.sync()
    thread = env.source.threads["1"]
    env.source.remove_thread("1")
    env.sync()
    assert env.query("SELECT archived_at FROM threads")[0][0] is not None
    env.source.threads["1"] = thread
    env.sync()
    assert env.query("SELECT archived_at FROM threads")[0][0] is None


def test_noise_rules_auto_see(env: Env) -> None:
    from dataclasses import replace
    import re
    from admentum_mcp.config import Rule
    env.cfg = replace(env.cfg, noise=(Rule(sender=re.compile("Skolmaten")),))
    env.source.add_thread("1", "Meny")
    env.source.add_message("1", "a", START, "x", sender_name="Skolmaten")
    r = env.sync()
    assert r["auto_seen"] == 1 and env.pending_ids() == []


def test_messages_without_ids_get_stable_hash_ids(env: Env) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", None, START, "x")
    env.sync()
    env.clock.advance(hours=1)
    env.source.add_message("1", "b", START + timedelta(minutes=5), "y")
    r = env.sync()
    assert r["new_replies"] == 1
    ids = [r["id"] for r in env.query("SELECT id FROM items ORDER BY position")]
    assert ids[0].startswith("sha-") and ids[1] == "b"


def test_seen_items_past_retention_are_pruned_unseen_kept(env: Env) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "seen", START - timedelta(days=10), "x")
    env.source.add_message("1", "unseen", START - timedelta(days=9), "y")
    env.sync()
    s = env.store()
    with s.tx():
        s.mark_seen("seen", "agent", "2026-01-01T00:00:00.000000Z")
    s.close()
    env.clock.advance(days=env.cfg.retention_days)
    r = env.sync()
    assert r["pruned_items"] == 1
    rows = {x["id"]: x for x in env.query("SELECT * FROM items")}
    assert rows["seen"]["pruned_at"] and rows["unseen"]["pruned_at"] is None
    assert not (env.data.data / rows["seen"]["path"]).exists()
    assert (env.data.data / rows["unseen"]["path"]).exists()


def test_threads_older_than_cutoff_are_not_fetched_until_new_activity(env: Env) -> None:
    lookback = env.cfg.sync.initial_lookback_days
    env.source.add_thread("ancient", "Gammal")
    env.source.add_message("ancient", "a1", START - timedelta(days=lookback + 50), "x")
    r = env.sync(dry_run=True)
    assert r["new_threads"] == 0 and r["changed_threads"] == 0
    r = env.sync()
    assert r["new_threads"] == 0 and r["changed_threads"] == 0
    assert env.source.fetches("ancient") == 0
    assert env.query("SELECT * FROM threads") == []  # not even the title is stored
    env.clock.advance(hours=1)
    env.sync()
    assert env.source.fetches("ancient") == 0

    env.source.add_message("ancient", "a2", env.clock.now, "nytt svar")
    env.clock.advance(hours=1)
    r = env.sync()
    assert env.source.fetches("ancient") == 1
    assert r["new_threads"] == 1 and r["new_items_in_new_threads"] == 1
    assert env.pending_ids() == ["a2"]  # the old message stays out
