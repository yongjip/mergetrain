"""Real SQLite regressions for observer cache freshness and lifetime."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from mergetrain.config import load_config, render_default_config
from mergetrain.dashboard import DashboardSnapshotCache, create_hub_server, create_server
from mergetrain.errors import QueueError
from mergetrain.hub import HubSnapshotCache, build_hub_snapshot
from mergetrain.store import connect, enqueue_job


def make_config(root: Path):
    (root / '.mergetrain.yaml').write_text(render_default_config('cache-test'))
    return load_config(repo=root)


@pytest.mark.parametrize('hub', [False, True])
def test_cache_sees_reused_wal_without_file_growth(tmp_path, hub):
    config = make_config(tmp_path)
    writer = connect(config.state.db)
    cache = HubSnapshotCache() if hub else DashboardSnapshotCache(config)
    def read():
        if hub:
            return build_hub_snapshot([{'path': str(tmp_path)}], cache=cache)['repos'][0]['snapshot']
        return cache()
    try:
        writer.execute('PRAGMA wal_autocheckpoint=0')
        job = enqueue_job(writer, task='queued', branch='codex/a')
        writer.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchall()
        assert read()['counts']['queued'] == 1
        wal = Path(f'{config.state.db}-wal')
        before = (config.state.db.stat().st_mtime_ns, wal.stat().st_size)
        writer.execute("UPDATE deploy_queue SET status='failed', note='gate failed' WHERE id=?", (job.id,))
        writer.commit()
        assert before == (config.state.db.stat().st_mtime_ns, wal.stat().st_size)
        assert read()['counts']['failed'] == 1
        assert read()['counts']['queued'] == 0
    finally:
        writer.close()
        cache.close()


def test_shared_observer_reuses_one_connection_and_does_not_pin_wal(tmp_path):
    from mergetrain.snapshot import build_dashboard_snapshot
    from mergetrain.snapshot_cache import connect as observer_connect

    config = make_config(tmp_path)
    writer = connect(config.state.db)
    cache = DashboardSnapshotCache(config)
    try:
        enqueue_job(writer, task='one', branch='codex/a')
        with patch('mergetrain.snapshot_cache.connect', wraps=observer_connect) as opened, patch(
            'mergetrain.dashboard.build_dashboard_snapshot', wraps=build_dashboard_snapshot
        ) as built:
            with ThreadPoolExecutor(max_workers=16) as pool:
                snapshots = list(pool.map(lambda _: cache(), range(64)))
            assert all(s['counts']['queued'] == 1 for s in snapshots)
            assert opened.call_count == 1
            assert built.call_count == 1
            observer = cache._monitor._conn
            assert observer is not None
            assert not observer.in_transaction
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                observer.execute("UPDATE deploy_queue SET task='forbidden'")
            # An idle observer must not pin a read transaction and prevent
            # truncation; the next commit is still observed after truncation.
            assert tuple(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()) == (0, 0, 0)
            enqueue_job(writer, task='two', branch='codex/b')
            assert cache()['counts']['queued'] == 2
            assert opened.call_count == 1
        cache.close()
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            observer.execute('SELECT 1')
        with pytest.raises(RuntimeError, match='closed'):
            cache()
    finally:
        writer.close()
        cache.close()


def test_commit_during_snapshot_build_invalidates_next_poll(tmp_path):
    from mergetrain.snapshot import build_dashboard_snapshot

    config = make_config(tmp_path)
    writer = connect(config.state.db)
    cache = DashboardSnapshotCache(config)
    try:
        job = enqueue_job(writer, task='one', branch='codex/a')
        def build_then_commit(*args, **kwargs):
            result = build_dashboard_snapshot(*args, **kwargs)
            writer.execute("UPDATE deploy_queue SET status='failed' WHERE id=?", (job.id,))
            writer.commit()
            return result
        with patch('mergetrain.dashboard.build_dashboard_snapshot', side_effect=build_then_commit):
            assert cache()['counts']['queued'] == 1
        assert cache()['counts']['failed'] == 1
    finally:
        writer.close()
        cache.close()


def test_missing_queue_observation_never_creates_files(tmp_path):
    config = make_config(tmp_path)
    cache = DashboardSnapshotCache(config)
    try:
        for _ in range(2):
            with pytest.raises(QueueError, match='does not exist'):
                cache()
        assert not config.state.db.parent.exists()
        assert cache._monitor._conn is None
        writer = connect(config.state.db)
        try:
            enqueue_job(writer, task='new', branch='codex/new')
        finally:
            writer.close()
        assert cache()['counts']['queued'] == 1
    finally:
        cache.close()


def test_hub_removal_and_config_db_change_release_observers(tmp_path):
    config = make_config(tmp_path)
    writer = connect(config.state.db)
    enqueue_job(writer, task='old', branch='codex/a')
    writer.close()
    cache = HubSnapshotCache()
    registered = [{'path': str(tmp_path)}]
    try:
        assert build_hub_snapshot(registered, cache=cache)['repos'][0]['snapshot']['counts']['queued'] == 1
        old = cache._monitors[str(tmp_path)]._conn
        (tmp_path / '.mergetrain.yaml').write_text(
            render_default_config('cache-test') + '\nstate:\n  db: new-queue/queue.sqlite\n'
        )
        assert build_hub_snapshot(registered, cache=cache)['repos'][0]['empty']
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            old.execute('SELECT 1')
        assert not (tmp_path / 'new-queue').exists()
        new_config = load_config(repo=tmp_path)
        writer = connect(new_config.state.db)
        enqueue_job(writer, task='new', branch='codex/b')
        writer.close()
        assert build_hub_snapshot(registered, cache=cache)['repos'][0]['snapshot']['counts']['queued'] == 1
        new = cache._monitors[str(tmp_path)]._conn
        build_hub_snapshot([], cache=cache)
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            new.execute('SELECT 1')
        assert not cache._monitors
        assert not cache._entries
    finally:
        cache.close()


@pytest.mark.parametrize('hub', [False, True])
def test_server_close_releases_shared_observer(tmp_path, hub):
    config = make_config(tmp_path)
    writer = connect(config.state.db)
    enqueue_job(writer, task='one', branch='codex/a')
    writer.close()
    if hub:
        cache = HubSnapshotCache()
        build_hub_snapshot([{'path': str(tmp_path)}], cache=cache)
        observer = cache._monitors[str(tmp_path)]._conn
        with patch('mergetrain.dashboard.HubSnapshotCache', return_value=cache):
            server = create_hub_server(port=0)
    else:
        cache = DashboardSnapshotCache(config)
        cache()
        observer = cache._monitor._conn
        with patch('mergetrain.dashboard.DashboardSnapshotCache', return_value=cache):
            server = create_server(config, port=0)
    server.server_close()
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        observer.execute('SELECT 1')
    # A late HTTP handler cannot reopen the cache after server shutdown.
    if hub:
        with pytest.raises(RuntimeError, match='closed'):
            cache.token(str(tmp_path), config.state.db)
    else:
        with pytest.raises(RuntimeError, match='closed'):
            cache()


def test_database_replacement_reopens_observer(tmp_path):
    import os

    if os.name == 'nt':
        pytest.skip('Windows disallows replacing an open SQLite file')
    config = make_config(tmp_path)
    writer = connect(config.state.db)
    enqueue_job(writer, task='old', branch='codex/a')
    writer.close()
    cache = DashboardSnapshotCache(config)
    try:
        assert cache()['jobs'][0]['task'] == 'old'
        old = cache._monitor._conn
        replacement = tmp_path / 'replacement.sqlite'
        writer = connect(replacement)
        enqueue_job(writer, task='new', branch='codex/b')
        writer.close()
        # Move the old main and its sidecars together, as a queue directory
        # replacement would do, so two unrelated databases never share a WAL.
        config.state.db.parent.rename(tmp_path / 'old-state')
        config.state.db.parent.mkdir()
        replacement.replace(config.state.db)
        assert cache()['jobs'][0]['task'] == 'new'
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            old.execute('SELECT 1')
    finally:
        cache.close()
