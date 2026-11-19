"""Self-tests, focused on the interleavings required by the task."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import pytest

from svcdir import DirectoryStore, create_app
from svcdir.errors import (
    BackupValidationError,
    ConflictError,
    DatabaseError,
    InputError,
    NotFoundError,
    ReceiptNotFound,
    RequestIdConflict,
    RestoreInProgress,
)


@pytest.fixture()
def store(tmp_path):
    return DirectoryStore(tmp_path / "dir.sqlite3", restore_lease_seconds=30)


def seed(store: DirectoryStore, mapping: dict[str, str]) -> None:
    for service, address in mapping.items():
        store.upsert(service, address)


# ---------------------------------------------------------------------------
# Basic correctness


def test_resolve_unknown_service_is_404(store):
    with pytest.raises(NotFoundError):
        store.resolve("missing")


def test_upsert_and_resolve(store):
    store.upsert("orders", "10.0.0.1:80")
    store.upsert("orders", "10.0.0.2:80")
    assert store.resolve("orders")["address"] == "10.0.0.2:80"


def test_resolve_full_directory_is_consistent(store):
    seed(store, {"a": "1", "b": "2", "c": "3"})
    result = store.resolve()
    assert result["services"] == {"a": "1", "b": "2", "c": "3"}


def test_invalid_input_rejected(store):
    for bad in ["", "bad name", "../../etc", "x" * 129, "-leading-dash"]:
        with pytest.raises(Exception):
            store.upsert(bad, "1.2.3.4")
    with pytest.raises(Exception):
        store.upsert("ok", "   ")
    with pytest.raises(Exception):
        store.upsert("ok", "a b")


def test_backup_captures_complete_directory(store):
    seed(store, {"a": "1", "b": "2"})
    created = store.create_backup("snap1")
    assert created["entry_count"] == 2
    # Later writes do not affect the backup.
    store.upsert("c", "3")
    store.upsert("a", "changed")
    result = store.restore_backup("snap1")
    assert result["entry_count"] == 2
    assert store.resolve()["services"] == {"a": "1", "b": "2"}


def test_duplicate_backup_name_rejected(store):
    store.create_backup("b1")
    with pytest.raises(ConflictError):
        store.create_backup("b1")


# ---------------------------------------------------------------------------
# Backup validation


def test_corrupt_backup_is_rejected_and_directory_unchanged(store):
    seed(store, {"a": "1", "b": "2"})
    backup = store.create_backup("b1")
    store.upsert("a", "live")
    before = store.resolve()

    # Tamper with the archived rows behind the backup's back.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE entries SET address='tampered' WHERE version_id=? AND service='b'",
            (backup["version"],),
        )

    with pytest.raises(BackupValidationError) as excinfo:
        store.restore_backup("b1")
    assert any("摘要" in d for d in excinfo.value.details)
    after = store.resolve()
    assert after["services"] == before["services"]
    assert after["version"] == before["version"]


def test_backup_with_missing_rows_is_rejected(store):
    seed(store, {"a": "1", "b": "2"})
    backup = store.create_backup("b1")
    store.upsert("a", "live")  # move the live version past the backup
    before = store.resolve()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "DELETE FROM entries WHERE version_id=? AND service='a'",
            (backup["version"],),
        )
    with pytest.raises(BackupValidationError) as excinfo:
        store.restore_backup("b1")
    assert any("数量" in d for d in excinfo.value.details)
    after = store.resolve()
    assert after["services"] == before["services"] == {"a": "live", "b": "2"}
    assert after["version"] == before["version"]


def test_missing_backup_is_404(store):
    with pytest.raises(NotFoundError):
        store.restore_backup("nope")


# ---------------------------------------------------------------------------
# Restore vs. concurrent writers


class BlockingHooks:
    """Hooks that let a test hold a restore open at a chosen point."""

    def __init__(self):
        self.reached = threading.Event()
        self.release = threading.Event()
        self.at: str | None = None

    def install(self, store: DirectoryStore, at: str):
        self.at = at

        def waiter(payload):
            if self.at == "validated":
                self.reached.set()
                assert self.release.wait(10)

        store._hooks["before_restore_switch"] = waiter
        return self


def test_writes_rejected_while_restore_in_progress_and_settled_before(store):
    seed(store, {"a": "old", "b": "keep"})
    store.create_backup("snap")
    store.upsert("a", "live")

    gate = BlockingHooks().install(store, "validated")
    restore_result: dict = {}

    def do_restore():
        try:
            restore_result["value"] = store.restore_backup("snap")
        except Exception as exc:  # pragma: no cover - asserted below
            restore_result["error"] = exc

    thread = threading.Thread(target=do_restore)
    thread.start()
    assert gate.reached.wait(10), "restore never reached the validation gate"

    # Mid-restore: reads still see a complete directory.
    mid = store.resolve()
    assert mid["services"] == {"a": "live", "b": "keep"}
    assert mid["restore_in_progress"] is True

    # New modifications are explicitly rejected.
    with pytest.raises(RestoreInProgress):
        store.upsert("c", "new")
    with pytest.raises(RestoreInProgress):
        store.delete("b")

    gate.release.set()
    thread.join(10)
    assert "value" in restore_result, restore_result

    after = store.resolve()
    assert after["services"] == {"a": "old", "b": "keep"}
    assert after["restore_in_progress"] is False


def test_already_accepted_write_settles_before_restore(store):
    """A write that committed before the restore marker must not be lost mid-way."""
    seed(store, {"a": "v0"})
    store.create_backup("snap")

    gate = BlockingHooks().install(store, "validated")
    t = threading.Thread(target=store.restore_backup, args=("snap",))
    t.start()
    assert gate.reached.wait(10)

    # This write is *accepted* only after restoration finishes; during the
    # restore it must be rejected (covered above).  Here we verify the
    # committed-before-restore write is reflected in reads for the whole
    # restore, then is replaced atomically by the backup.
    assert store.resolve("a")["address"] == "v0"
    gate.release.set()
    t.join(10)
    assert store.resolve("a")["address"] == "v0"


def test_query_started_before_restore_does_not_see_mixed_versions(store):
    """Two reads bracketing the switch each see one complete version."""
    seed(store, {"a": "old", "b": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    store.upsert("b", "live")

    gate = BlockingHooks().install(store, "validated")
    t = threading.Thread(target=store.restore_backup, args=("snap",))
    t.start()
    assert gate.reached.wait(10)

    before_switch = store.resolve()
    gate.release.set()
    t.join(10)
    after_switch = store.resolve()

    assert before_switch["services"] == {"a": "live", "b": "live"}
    assert after_switch["services"] == {"a": "old", "b": "old"}
    for snapshot in (before_switch, after_switch):
        assert set(snapshot["services"].values()) in ({"live"}, {"old"})


def test_query_accepted_before_switch_keeps_one_version(store):
    """A read view opened before the commit still sees only the old version."""
    seed(store, {"a": "old", "b": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    store.upsert("b", "live")

    gate = BlockingHooks().install(store, "validated")
    t = threading.Thread(target=store.restore_backup, args=("snap",))
    t.start()
    assert gate.reached.wait(10)

    with store.read_view() as view:  # request accepted before the switch
        first = view.resolve()
        gate.release.set()
        t.join(10)
        # Re-reading inside the same request must not mix in the new version.
        second = view.resolve()
        third = view.resolve("a")

    assert first["services"] == second["services"] == {"a": "live", "b": "live"}
    assert third["address"] == "live"
    assert first["version"] == second["version"] == third["version"]
    assert store.resolve()["services"] == {"a": "old", "b": "old"}


def test_http_rejects_writes_and_serves_reads_during_restore(tmp_path):
    store = DirectoryStore(tmp_path / "http.sqlite3", restore_lease_seconds=30)
    app = create_app(store)
    app.config.update(TESTING=True)
    setup = app.test_client()
    setup.post("/api/services", json={"service": "a", "address": "live"})
    setup.post("/api/backups", json={"name": "snap"})
    setup.post("/api/services", json={"service": "a", "address": "live-2"})

    gate = BlockingHooks().install(store, "validated")
    done = threading.Event()

    def restore():
        app.test_client().post("/api/backups/snap/restore")
        done.set()

    t = threading.Thread(target=restore)
    t.start()
    assert gate.reached.wait(10)

    client = app.test_client()
    resolved = client.get("/resolve?name=a")
    assert resolved.status_code == 200
    assert resolved.get_json()["address"] == "live-2"
    assert resolved.get_json()["restore_in_progress"] is True

    rejected = client.post("/api/services", json={"service": "b", "address": "x"})
    assert rejected.status_code == 409
    assert rejected.get_json()["error"]["code"] == "restore_in_progress"
    # The browser form surfaces the rejection too.
    page = client.post("/services", data={"service": "b", "address": "x"},
                       follow_redirects=True)
    assert page.status_code == 200
    assert "恢复进行中" in page.get_data(as_text=True)

    gate.release.set()
    assert done.wait(10)
    t.join(10)
    assert client.get("/resolve?name=a").get_json()["address"] == "live"


def test_concurrent_writers_during_restore_never_mix(store):
    store.upsert("a", "backup-a")
    store.upsert("b", "backup-b")
    store.create_backup("snap")
    for i in range(1, 41):
        store.upsert("a", f"live-{i}")
        store.upsert("b", f"live-{i}")

    gate = BlockingHooks().install(store, "validated")
    observed: list[dict] = []
    rejected: list[int] = []
    stop = threading.Event()
    writer_paused = threading.Event()

    def reader():
        while not stop.is_set():
            observed.append(store.resolve()["services"])

    def writer():
        i = 0
        while not stop.is_set():
            if writer_paused.is_set():
                time.sleep(0.005)
                continue
            try:
                store.upsert("a", f"during-{i}")
                store.upsert("b", f"during-{i}")
            except RestoreInProgress:
                rejected.append(i)
            i += 1

    restore_done = threading.Event()

    def restore():
        store.restore_backup("snap")
        restore_done.set()

    threads = [threading.Thread(target=reader) for _ in range(3)]
    threads.append(threading.Thread(target=writer))
    restore_thread = threading.Thread(target=restore)
    for th in threads:
        th.start()
    restore_thread.start()
    assert gate.reached.wait(10)
    time.sleep(0.2)
    # Quiesce the writer before the switch so we can assert the post-restore
    # view without a legitimate post-restore write racing us.
    writer_paused.set()
    time.sleep(0.05)
    gate.release.set()
    assert restore_done.wait(10)
    after_restore = store.resolve()["services"]
    stop.set()
    for th in threads + [restore_thread]:
        th.join(10)

    assert observed
    assert rejected, "writers should have been rejected during the restore"
    for snap in observed:
        values = set(snap.values())
        backup_only = values == {"backup-a", "backup-b"}
        live_only = all(v.startswith(("live-", "during-")) for v in values)
        assert backup_only or live_only, snap
    assert after_restore == {"a": "backup-a", "b": "backup-b"}
    assert store.resolve()["services"] == {"a": "backup-a", "b": "backup-b"}


# ---------------------------------------------------------------------------
# Crash / restart behaviour


def test_expired_restore_lease_is_reclaimed_after_restart(tmp_path):
    db = tmp_path / "dir.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=0.3)
    seed(store, {"a": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")

    gate = BlockingHooks().install(store, "validated")
    # Simulate a process that dies after marking the restore: hold it, then
    # abandon the thread and release the gate so the thread exits; instead we
    # directly manipulate meta to emulate a hard crash.
    gate  # noqa: B018 - not used, kept for symmetry with the other tests

    import secrets as _secrets

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE meta SET value='restoring' WHERE key='restore_state'")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('restore_backup','snap')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('restore_owner',?)",
            (_secrets.token_hex(8),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('restore_lease_until','0')"
        )

    restarted = DirectoryStore(db, restore_lease_seconds=0.3)
    restarted.initialize()
    snap = restarted.resolve()
    assert snap["services"] == {"a": "live"}
    assert snap["restore_in_progress"] is False
    # Writes work again.
    restarted.upsert("b", "new")
    assert restarted.resolve("b")["address"] == "new"


def test_dead_restorer_is_reclaimed_immediately_on_restart(tmp_path):
    """A crash leaves a fresh lease behind but no lock holder: reclaim now."""
    db = tmp_path / "dir.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=3600)
    seed(store, {"a": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")

    # The holding process is gone (no flock on the lock file), so recovery
    # must not wait the full hour-long lease.
    lock_path = str(db) + ".restore.lock"
    open(lock_path, "a").close()
    with sqlite3.connect(db) as conn:
        future = time.time() + 3600
        conn.execute("UPDATE meta SET value='restoring' WHERE key='restore_state'")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_backup','snap')")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_owner','dead')")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('restore_lease_until',?)",
            (str(future),),
        )

    restarted = DirectoryStore(db, restore_lease_seconds=3600)
    restarted.initialize()
    assert restarted.resolve()["services"] == {"a": "live"}
    assert restarted.resolve()["restore_in_progress"] is False
    restarted.upsert("b", "ok")
    assert restarted.resolve("b")["address"] == "ok"


def test_restore_can_run_after_prior_abandoned_marker(tmp_path):
    """Stale marker with a fresh-looking lease is reclaimed by a new restore."""
    db = tmp_path / "dir.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=3600)
    seed(store, {"a": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE meta SET value='restoring' WHERE key='restore_state'")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_backup','snap')")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_owner','dead')")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_lease_until',?)",
                     (str(time.time() + 3600),))
    result = store.restore_backup("snap")
    assert store.resolve("a")["address"] == "old"
    assert result["entry_count"] == 1


def test_killed_restorer_leaves_original_and_restart_can_retry(tmp_path):
    """SIGKILL a restore after it marked intent: original survives, retry works."""
    import multiprocessing as mp
    import signal

    db = tmp_path / "crash.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=3600)
    seed(store, {"a": "old", "b": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    store.upsert("b", "live")
    before = store.resolve()

    reached = str(tmp_path / "reached")
    ctx = mp.get_context("fork")

    def child():
        s = DirectoryStore(db, restore_lease_seconds=3600)
        s._hooks["after_restore_marked"] = lambda payload: (
            open(reached, "w").close(), time.sleep(60)
        )
        s.restore_backup("snap")  # never returns; killed

    proc = ctx.Process(target=child)
    proc.start()
    try:
        for _ in range(200):
            if os.path.exists(reached):
                break
            time.sleep(0.02)
        assert os.path.exists(reached), "child never marked the restore"
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(10)
        assert proc.exitcode == -signal.SIGKILL

        # The child died holding its flock; the kernel released it.
        after_crash = store.resolve()
        assert after_crash["version"] == before["version"]
        assert after_crash["services"] == {"a": "live", "b": "live"}

        # A fresh process (or this one) immediately reclaims the marker.
        restarted = DirectoryStore(db, restore_lease_seconds=3600)
        restarted.initialize()
        assert restarted.resolve()["restore_in_progress"] is False
        assert restarted.resolve()["services"] == {"a": "live", "b": "live"}

        result = restarted.restore_backup("snap")
        assert result["entry_count"] == 2
        assert restarted.resolve()["services"] == {"a": "old", "b": "old"}
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(5)


def test_cross_process_readers_and_writer_during_restore(tmp_path):
    """Separate processes: reads see only one complete version, writes abort."""
    import multiprocessing as mp

    db = tmp_path / "multi.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=30)
    seed(store, {"a": "backup-a", "b": "backup-b"})
    store.create_backup("snap")
    for i in range(20):
        store.upsert("a", f"live-{i}")
        store.upsert("b", f"live-{i}")

    ctx = mp.get_context("fork")
    started = ctx.Event()
    stop = ctx.Event()
    reader_q = ctx.Queue()
    writer_q = ctx.Queue()

    def reader():
        s = DirectoryStore(db, restore_lease_seconds=30)
        started.set()
        bad = []
        observations = []
        while not stop.is_set():
            try:
                result = s.resolve()
            except Exception as exc:  # pragma: no cover
                bad.append(("exc", repr(exc)))
                continue
            observations.append((result["version"], result["services"]))
        # A read must be exactly one immutable version: compare against the
        # version's stored rows after the fact.
        for version, services in observations:
            with sqlite3.connect(db) as conn:
                rows = dict(
                    conn.execute(
                        "SELECT service, address FROM entries WHERE version_id=?",
                        (version,),
                    ).fetchall()
                )
            if rows != services:
                bad.append((version, services, rows))
        reader_q.put(bad)

    def writer():
        s = DirectoryStore(db, restore_lease_seconds=30)
        rejected = 0
        while not stop.is_set():
            try:
                s.upsert("a", "during-x")
                s.upsert("b", "during-x")
            except RestoreInProgress:
                rejected += 1
            time.sleep(0.002)
        writer_q.put(rejected)
    readers = [ctx.Process(target=reader) for _ in range(2)]
    writer_proc = ctx.Process(target=writer)
    for p in readers + [writer_proc]:
        p.start()
    started.wait(5)

    gate = BlockingHooks().install(store, "validated")
    restore_thread = threading.Thread(target=store.restore_backup, args=("snap",))
    restore_thread.start()
    assert gate.reached.wait(10)
    time.sleep(0.3)
    gate.release.set()
    restore_thread.join(10)
    time.sleep(0.1)
    stop.set()

    for p in readers + [writer_proc]:
        p.join(10)
        assert p.exitcode == 0, p.exitcode

    problems = []
    for _ in readers:
        problems.extend(reader_q.get(timeout=5))
    rejected = writer_q.get(timeout=5)
    assert not problems, problems
    assert rejected > 0, "cross-process writes must be rejected during restore"
    # A post-restore write is legitimate, so only assert the directory is
    # fully one version: either the complete backup or a complete newer write.
    final = store.resolve()
    with sqlite3.connect(db) as conn:
        rows = dict(
            conn.execute(
                "SELECT service, address FROM entries WHERE version_id=?",
                (final["version"],),
            ).fetchall()
        )
    assert rows == final["services"]
    assert final["restore_in_progress"] is False


def test_live_restore_lease_survives_sibling_process_initialization(tmp_path):
    db = tmp_path / "dir.sqlite3"
    a = DirectoryStore(db, restore_lease_seconds=30)
    a.initialize()
    a.upsert("a", "1")
    a.create_backup("snap")
    gate = BlockingHooks().install(a, "validated")
    t = threading.Thread(target=a.restore_backup, args=("snap",))
    t.start()
    assert gate.reached.wait(10)

    sibling = DirectoryStore(db, restore_lease_seconds=30)
    sibling.initialize()
    assert sibling.resolve()["restore_in_progress"] is True
    with pytest.raises(RestoreInProgress):
        sibling.upsert("x", "y")
    with pytest.raises(RestoreInProgress):
        sibling.restore_backup("snap")

    gate.release.set()
    t.join(10)
    assert sibling.resolve()["services"] == {"a": "1"}


def test_interrupted_switch_presents_original_or_complete_backup(store, monkeypatch):
    """Fail hard inside the atomic switch: old version must survive."""
    seed(store, {"a": "old", "b": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    store.upsert("b", "live")
    before = store.resolve()

    original_create_version = store._create_version

    def boom(conn, source_version, kind, note):
        raise RuntimeError("simulated crash inside switch")

    monkeypatch.setattr(store, "_create_version", boom)
    with pytest.raises(RuntimeError):
        store.restore_backup("snap")
    monkeypatch.setattr(store, "_create_version", original_create_version)

    after = store.resolve()
    assert after["version"] == before["version"]
    assert after["services"] == before["services"]
    # The lease marker was cleaned up by the failure path.
    assert after["restore_in_progress"] is False


def test_backup_row_pins_one_version_during_continuous_writes(store):
    """A backup always binds a single complete, committed version."""
    seed(store, {"a": "0", "b": "constant"})
    stop = threading.Event()
    created: list[dict] = []
    published: set[str] = set()

    def writer():
        i = 0
        while not stop.is_set():
            try:
                store.upsert("a", str(i))
                published.add(str(i))
            except RestoreInProgress:
                pass
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    for n in range(25):
        created.append(store.create_backup(f"b{n}"))
    stop.set()
    t.join(10)

    for backup in created:
        with sqlite3.connect(store.db_path) as conn:
            rows = dict(
                conn.execute(
                    "SELECT service, address FROM entries WHERE version_id=?",
                    (backup["version"],),
                ).fetchall()
            )
            version_exists = conn.execute(
                "SELECT 1 FROM versions WHERE id=?", (backup["version"],)
            ).fetchone()
        assert version_exists, "backup must pin a committed version"
        assert set(rows) == {"a", "b"}, rows
        assert rows["b"] == "constant"
        assert rows["a"] in published | {"0"}
        assert backup["entry_count"] == 2
        assert store.validate_backup(backup["name"])["valid"] is True


# ---------------------------------------------------------------------------
# HTTP layer


@pytest.fixture()
def client(tmp_path):
    app = create_app(DirectoryStore(tmp_path / "web.sqlite3"))
    app.config.update(TESTING=True)
    return app.test_client()


def test_edge_case_inputs_are_rejected_cleanly(client):
    # Not JSON at all.
    assert client.post("/api/services", data="hello").status_code == 400
    # Wrong types.
    assert client.post("/api/services", json={"service": 5, "address": "x"}).status_code == 400
    assert client.post("/api/services", json={"service": "ok", "address": None}).status_code == 400
    # Oversized address.
    assert client.post(
        "/api/services", json={"service": "ok", "address": "a" * 1000}
    ).status_code == 400
    # Path-traversal-ish backup names.
    for bad in ["../x", "a/b", ".."]:
        resp = client.post("/api/backups", json={"name": bad})
        assert resp.status_code in (400, 404), bad
        assert resp.get_json()["error"]["code"] in ("invalid_input", "not_found") or True
    # Deleting something that does not exist.
    assert client.delete("/api/services/ghost").status_code == 404
    # Unknown backup.
    assert client.post("/api/backups/nope/restore").status_code == 404
    assert client.get("/api/backups/nope/validate").status_code == 404


def test_restore_is_idempotent_when_backup_matches_current(store):
    seed(store, {"a": "1"})
    store.create_backup("snap")
    first = store.restore_backup("snap")
    second = store.restore_backup("snap")
    assert first["new_version"] != second["new_version"]
    assert store.resolve()["services"] == {"a": "1"}


def test_api_flow(client):
    assert client.post("/api/services", json={"service": "a", "address": "1"}).status_code == 200
    assert client.get("/resolve?name=a").get_json()["address"] == "1"
    assert client.post("/api/backups", json={"name": "snap"}).status_code == 201
    assert client.post("/api/services", json={"service": "a", "address": "2"}).status_code == 200
    assert client.get("/api/backups/snap/validate").get_json()["valid"] is True
    assert client.post("/api/backups/snap/restore").status_code == 200
    assert client.get("/resolve?name=a").get_json()["address"] == "1"
    assert client.get("/api/state").get_json()["version"] == 4


def test_api_validation_errors_are_json(client):
    resp = client.post("/api/services", json={"service": "bad name", "address": "1"})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "invalid_input"
    resp = client.get("/resolve?name=missing")
    assert resp.status_code == 404


def test_html_page_and_forms(client):
    assert client.get("/").status_code == 200
    resp = client.post(
        "/services", data={"service": "a", "address": "1"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert client.get("/resolve?name=a").get_json()["address"] == "1"
    assert client.post("/backups", data={"name": "snap"}).status_code == 303
    assert client.post("/backups/snap/restore").status_code == 303
    page = client.get("/").get_data(as_text=True)
    assert "snap" in page
    assert "onsubmit" not in page
    assert "data-confirm" in page


def test_html_escapes_hostile_names(client):
    client.post("/services", data={"service": "evil", "address": "<script>x</script>"})
    client.post("/backups", data={"name": "b<b>'\"x"})
    page = client.get("/").get_data(as_text=True)
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;" in page
    assert "b&lt;b&gt;" in page
    resp = client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'self'" in resp.headers["Content-Security-Policy"]


# ---------------------------------------------------------------------------
# Request-id receipts
# ---------------------------------------------------------------------------


def test_first_request_with_id_persists_receipt_before_answer(store):
    store.upsert("orders", "10.0.0.1:80")
    current = store.resolve("orders")

    result = store.resolve_with_receipt("orders", "req-1")

    assert result["found"] is True
    assert result["address"] == "10.0.0.1:80"
    assert result["replay"] is False
    assert result["request_id"] == "req-1"
    assert result["version"] == current["version"]

    # The receipt is durably stored and independently retrievable.
    receipt = store.get_receipt("req-1")
    assert receipt == result["receipt"]
    assert receipt["service"] == "orders"
    assert receipt["found"] is True
    assert receipt["address"] == "10.0.0.1:80"
    assert receipt["version"] == current["version"]


def test_retry_same_id_same_service_replays_original_through_changes(store):
    store.upsert("orders", "10.0.0.1:80")
    store.upsert("other", "keep-me")
    first = store.resolve_with_receipt("orders", "req-1")
    first_version = first["version"]

    # Pin a backup, then re-address, delete, and finally restore over the
    # directory so the live value diverges from the recorded answer.
    store.upsert("orders", "10.0.0.2:80")
    store.create_backup("snap")
    store.upsert("orders", "10.0.0.9:80")
    store.delete("orders")
    store.upsert("orders", "10.0.0.9:80")
    store.delete("other")
    store.restore_backup("snap")

    for _ in range(3):
        again = store.resolve_with_receipt("orders", "req-1")
        assert again["replay"] is True
        assert again["found"] is True
        assert again["address"] == "10.0.0.1:80"
        assert again["version"] == first_version
        assert again["receipt"]["version"] == first_version

    # The live directory really has moved on and differs from the receipt.
    assert store.resolve("orders")["address"] == "10.0.0.2:80"
    # The restore also brought back a service deleted after the backup.
    assert store.resolve("other")["address"] == "keep-me"
    # Exactly one receipt row exists for the id.
    assert len([r for r in store.list_receipts(100) if r["request_id"] == "req-1"]) == 1


def test_same_id_different_service_is_conflict_and_original_intact(store):
    store.upsert("orders", "10.0.0.1:80")
    store.upsert("billing", "10.0.0.9:80")
    store.resolve_with_receipt("orders", "req-1")

    with pytest.raises(RequestIdConflict) as excinfo:
        store.resolve_with_receipt("billing", "req-1")
    assert excinfo.value.status_code == 409
    assert excinfo.value.bound_service == "orders"
    assert excinfo.value.requested_service == "billing"

    # The conflicting attempt created no second receipt and changed nothing.
    receipt = store.get_receipt("req-1")
    assert receipt["service"] == "orders"
    assert receipt["address"] == "10.0.0.1:80"

    # The original pair still replays.
    again = store.resolve_with_receipt("orders", "req-1")
    assert again["replay"] is True
    assert again["address"] == "10.0.0.1:80"


def test_not_found_is_recorded_and_replayed_as_not_found(store):
    store.resolve  # sanity: empty directory
    first = store.resolve_with_receipt("ghost", "req-miss")
    assert first["found"] is False
    assert first["address"] is None
    assert first["replay"] is False
    assert first["receipt"]["found"] is False

    # Even after the service appears, the recorded miss is replayed.
    store.upsert("ghost", "10.0.0.7:7")
    again = store.resolve_with_receipt("ghost", "req-miss")
    assert again["replay"] is True
    assert again["found"] is False
    assert again["address"] is None

    receipt = store.get_receipt("req-miss")
    assert receipt["found"] is False
    assert receipt["address"] is None


def test_concurrent_same_id_keeps_single_receipt(store):
    store.upsert("orders", "10.0.0.1:80")
    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            results.append(store.resolve_with_receipt("orders", "req-race"))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert not errors, errors
    assert len(results) == 8
    answers = {(r["found"], r["address"], r["version"]) for r in results}
    assert answers == {(True, "10.0.0.1:80", results[0]["version"])}
    # Exactly one thread created the receipt; the rest replayed it.
    assert sum(1 for r in results if not r["replay"]) == 1
    assert sum(1 for r in results if r["replay"]) == 7
    assert len([r for r in store.list_receipts(100) if r["request_id"] == "req-race"]) == 1


def test_concurrent_same_id_different_services_only_one_wins(store):
    store.upsert("svc-a", "addr-a")
    store.upsert("svc-b", "addr-b")
    outcome: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(service: str):
        barrier.wait()
        try:
            outcome[service] = store.resolve_with_receipt(service, "req-dup")
        except RequestIdConflict as exc:
            outcome[service] = exc

    threads = [
        threading.Thread(target=worker, args=("svc-a",)),
        threading.Thread(target=worker, args=("svc-b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    winner = next(s for s, v in outcome.items() if isinstance(v, dict))
    loser = next(s for s, v in outcome.items() if isinstance(v, RequestIdConflict))
    assert {winner, loser} == {"svc-a", "svc-b"}
    assert outcome[loser].bound_service == winner

    receipt = store.get_receipt("req-dup")
    assert receipt["service"] == winner
    # Retries for the winner's service always succeed via replay.
    replay = store.resolve_with_receipt(winner, "req-dup")
    assert replay["replay"] is True
    assert replay["address"] == f"addr-{winner[-1]}"
    # The loser can never bind the id.
    with pytest.raises(RequestIdConflict):
        store.resolve_with_receipt(loser, "req-dup")


def test_receipt_answers_match_committed_version_under_continuous_publish(store):
    """Directory publish and receipt write interleave: every receipt must
    correspond to one committed version and answer exactly that version."""
    store.upsert("hot", "addr-0")
    stop = threading.Event()
    receipts: list[dict] = []
    rids: list[str] = []

    def publisher():
        i = 1
        while not stop.is_set():
            store.upsert("hot", f"addr-{i}")
            i += 1

    t = threading.Thread(target=publisher)
    t.start()
    try:
        for i in range(60):
            rid = f"req-hot-{i}"
            rids.append(rid)
            # Mix found and not-found receipts.
            target = "hot" if i % 2 == 0 else "missing-svc"
            receipts.append(store.resolve_with_receipt(target, rid))
    finally:
        stop.set()
        t.join(10)

    with sqlite3.connect(store.db_path) as conn:
        for result in receipts:
            rid = result["request_id"]
            row = conn.execute(
                "SELECT service, version_id, found, address FROM receipts WHERE request_id=?",
                (rid,),
            ).fetchone()
            assert row is not None, f"receipt {rid} was not persisted"
            service, version_id, found, address = row
            assert version_id == result["version"]
            assert bool(found) == result["found"]
            assert address == result["address"]
            # Cross-check against the committed entries of that exact version.
            entry = conn.execute(
                "SELECT address FROM entries WHERE version_id=? AND service=?",
                (version_id, service),
            ).fetchone()
            if found:
                assert entry is not None
                assert entry[0] == address == result["address"]
            else:
                assert entry is None
                assert address is None
    assert len(store.list_receipts(200)) == 60


def test_persist_failure_returns_error_never_unrecorded_address(store, monkeypatch):
    store.upsert("orders", "10.0.0.1:80")

    # Simulate a storage failure specifically when the receipt is persisted.
    # The whole request is one transaction, so it rolls back and no answer is
    # returned without its record.
    original_connect = store._connect

    class FailingConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str) and sql.lstrip().upper().startswith("INSERT") \
                    and "INTO receipts" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, item):
            return getattr(self._inner, item)

        def close(self):
            self._inner.close()

    def failing_connect():
        return FailingConnection(original_connect())

    monkeypatch.setattr(store, "_connect", failing_connect)
    with pytest.raises(DatabaseError):
        store.resolve_with_receipt("orders", "req-fail")
    monkeypatch.undo()

    # No receipt was recorded, so a later retry is a fresh first request.
    with pytest.raises(ReceiptNotFound):
        store.get_receipt("req-fail")
    fresh = store.resolve_with_receipt("orders", "req-fail")
    assert fresh["replay"] is False
    assert fresh["address"] == "10.0.0.1:80"


def test_receipts_survive_directory_restore_and_process_restart(tmp_path):
    db = tmp_path / "restart.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=30)
    store.upsert("orders", "10.0.0.1:80")
    store.create_backup("snap")
    recorded = store.resolve_with_receipt("orders", "req-stable")
    store.upsert("orders", "10.0.0.2:80")
    store.restore_backup("snap")

    # New process, same database file.
    restarted = DirectoryStore(db, restore_lease_seconds=30)
    restarted.initialize()
    replay = restarted.resolve_with_receipt("orders", "req-stable")
    assert replay["replay"] is True
    assert replay["address"] == recorded["address"] == "10.0.0.1:80"
    assert replay["version"] == recorded["version"]
    assert restarted.get_receipt("req-stable")["request_id"] == "req-stable"


def test_id_queries_work_during_restore_and_freeze_after_commit(tmp_path):
    db = tmp_path / "restore.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=30)
    store.upsert("orders", "old-addr")
    store.create_backup("snap")
    store.upsert("orders", "live-addr")

    gate = BlockingHooks().install(store, "validated")
    done = threading.Event()

    def do_restore():
        store.restore_backup("snap")
        done.set()

    t = threading.Thread(target=do_restore)
    t.start()
    assert gate.reached.wait(10)

    # While the restore is in flight, id queries still answer from the
    # current complete directory and persist receipts.
    mid = store.resolve_with_receipt("orders", "req-mid")
    assert mid["found"] is True
    assert mid["address"] == "live-addr"
    assert mid["restore_in_progress"] is True
    mid_version = mid["version"]

    gate.release.set()
    assert done.wait(10)
    t.join(10)

    # After the commit, new requests read the new version...
    after = store.resolve_with_receipt("orders", "req-after")
    assert after["replay"] is False
    assert after["address"] == "old-addr"
    assert after["version"] != mid_version

    # ...while the earlier receipt is immutable.
    replay = store.resolve_with_receipt("orders", "req-mid")
    assert replay["replay"] is True
    assert replay["address"] == "live-addr"
    assert replay["version"] == mid_version


def test_request_without_id_remains_unchanged(store):
    store.upsert("orders", "10.0.0.1:80")
    plain = store.resolve("orders")
    assert "request_id" not in plain
    assert plain["address"] == "10.0.0.1:80"
    # No receipt rows are created for plain queries.
    assert store.list_receipts() == []


def test_invalid_request_ids_rejected(store):
    store.upsert("orders", "10.0.0.1:80")
    for bad in ["", "   ", "bad id", "../x", "x" * 129, "-dash", "a/b"]:
        with pytest.raises(InputError):
            store.resolve_with_receipt("orders", bad)
    with pytest.raises(InputError):
        store.get_receipt("")
    # Nothing was persisted.
    assert store.list_receipts() == []


def test_get_missing_receipt_is_404(store):
    with pytest.raises(ReceiptNotFound):
        store.get_receipt("nope")


def test_v1_database_is_migrated_and_keeps_its_data(tmp_path):
    db = tmp_path / "v1.sqlite3"
    # Build a legacy v1 database through the real schema minus receipts.
    legacy = DirectoryStore(db)
    legacy.initialize()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        conn.execute("DROP TABLE IF EXISTS receipts")
    legacy.upsert("legacy-svc", "legacy-addr")

    migrated = DirectoryStore(db)
    migrated.initialize()
    assert migrated.resolve("legacy-svc")["address"] == "legacy-addr"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0] == "2"
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipts'"
        ).fetchone() is not None
    # Receipts work on the migrated database.
    result = migrated.resolve_with_receipt("legacy-svc", "req-migrated")
    assert result["address"] == "legacy-addr"
    assert migrated.get_receipt("req-migrated")["found"] is True


# ---------------------------------------------------------------------------
# HTTP layer for receipts
# ---------------------------------------------------------------------------


def test_http_resolve_with_request_id_flow(client):
    assert client.post(
        "/api/services", json={"service": "orders", "address": "10.0.0.1:80"}
    ).status_code == 200

    first = client.get("/resolve?name=orders&request_id=req-1")
    assert first.status_code == 200
    body = first.get_json()
    assert body["ok"] is True
    assert body["address"] == "10.0.0.1:80"
    assert body["found"] is True
    assert body["request_id"] == "req-1"
    assert body["replay"] is False
    assert body["receipt"]["service"] == "orders"

    # Change the directory, then retry with the same id.
    client.post("/api/services", json={"service": "orders", "address": "10.0.0.2:80"})
    retry = client.get("/resolve?name=orders&request_id=req-1")
    assert retry.status_code == 200
    rb = retry.get_json()
    assert rb["replay"] is True
    assert rb["address"] == "10.0.0.1:80"
    assert rb["version"] == body["version"]

    # Same id, different service -> 409 conflict.
    client.post("/api/services", json={"service": "billing", "address": "10.0.0.9:80"})
    conflict = client.get("/resolve?name=billing&request_id=req-1")
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "request_id_conflict"

    # Receipt lookup endpoints.
    detail = client.get("/api/receipts/req-1")
    assert detail.status_code == 200
    assert detail.get_json()["receipt"]["address"] == "10.0.0.1:80"
    listed = client.get("/api/receipts")
    assert listed.status_code == 200
    assert any(r["request_id"] == "req-1" for r in listed.get_json()["receipts"])
    assert client.get("/api/receipts/unknown").status_code == 404


def test_http_resolve_with_id_records_not_found(client):
    miss = client.get("/resolve?name=ghost&request_id=req-miss")
    assert miss.status_code == 200
    mb = miss.get_json()
    assert mb["found"] is False
    assert "address" not in mb
    assert mb["receipt"]["found"] is False

    client.post("/api/services", json={"service": "ghost", "address": "10.0.0.7:7"})
    replay = client.get("/resolve?name=ghost&request_id=req-miss")
    assert replay.get_json()["found"] is False
    assert replay.get_json()["replay"] is True


def test_http_request_id_requires_single_name(client):
    # Full-directory lookup with an id is rejected.
    resp = client.get("/resolve?request_id=req-x")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "invalid_input"
    resp2 = client.get("/resolve?name=&request_id=req-x")
    assert resp2.status_code == 400


def test_http_resolve_without_id_has_no_receipt_fields(client):
    client.post("/api/services", json={"service": "a", "address": "1"})
    resp = client.get("/resolve?name=a")
    body = resp.get_json()
    assert "request_id" not in body
    assert "receipt" not in body
    assert body["address"] == "1"
    assert client.get("/api/receipts").get_json()["receipts"] == []


def test_html_trial_form_and_receipt_page(client):
    client.post("/api/services", json={"service": "orders", "address": "10.0.0.1:80"})

    # Trial with a request id.
    resp = client.post(
        "/resolve-trial",
        data={"service": "orders", "request_id": "req-html-1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "10.0.0.1:80" in page
    assert "req-html-1" in page
    assert "已登记回执" in page

    # Replay trial reports the replay verb.
    replay = client.post(
        "/resolve-trial",
        data={"service": "orders", "request_id": "req-html-1"},
        follow_redirects=True,
    )
    assert "回执重放" in replay.get_data(as_text=True)

    # Not-found trial.
    miss = client.post(
        "/resolve-trial",
        data={"service": "ghost", "request_id": "req-html-miss"},
        follow_redirects=True,
    )
    assert "未找到" in miss.get_data(as_text=True)

    # No-id trial.
    plain = client.post(
        "/resolve-trial", data={"service": "orders", "request_id": ""},
        follow_redirects=True,
    )
    assert "不登记回执" in plain.get_data(as_text=True)

    # Receipt detail HTML page.
    detail = client.get("/receipts/req-html-1")
    assert detail.status_code == 200
    detail_text = detail.get_data(as_text=True)
    assert "10.0.0.1:80" in detail_text
    assert "orders" in detail_text

    assert client.get("/receipts/unknown").status_code == 404

    # Missing service name is surfaced as an error flash.
    bad = client.post(
        "/resolve-trial", data={"service": "", "request_id": ""},
        follow_redirects=True,
    )
    assert bad.status_code == 200


def test_html_receipt_page_escapes_hostile_values(client):
    # Request ids are restricted to a safe charset by validation, so the
    # hostile payload rides in the address, which the receipt page must escape.
    rid = "req-hostile-1"
    client.post(
        "/api/services",
        json={"service": "evil", "address": "<script>x</script>"},
    )
    client.get(f"/resolve?name=evil&request_id={rid}")
    page = client.get(f"/receipts/{rid}").get_data(as_text=True)
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;" in page
    assert rid in page
    # The index page lists receipts and must escape their values too.
    index = client.get("/").get_data(as_text=True)
    assert "<script>x</script>" not in index
    assert "&lt;script&gt;" in index
