from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from mergetrain.config import load_config
from mergetrain.hub_daemon import hub_daemon_loop, hub_sweep
from mergetrain.registry import add_repo, load_registry
from mergetrain.store import connect, enqueue_job, list_jobs


def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    (repo / ".mergetrain.yaml").write_text(f"project:\n  name: {name}\n", encoding="utf-8")
    return repo


def seed_jobs(repo: Path, *, auto: bool) -> int:
    config = load_config(repo=repo)
    conn = connect(config.state.db)
    try:
        job = enqueue_job(
            conn,
            task=f"{repo.name}-task",
            branch=f"agent/{repo.name}",
            worktree_path=str(repo),
            auto_deploy=auto,
        )
        return job.id
    finally:
        conn.close()


def recording_factory(log: list):
    def factory(config, owner):
        def process_batch(conn, jobs):
            start = time.monotonic()
            time.sleep(0.05)
            log.append((config.project.name, [job.id for job in jobs], start, time.monotonic()))

        return process_batch

    return factory


class HubSweepTests(unittest.TestCase):
    def test_sweep_processes_auto_jobs_per_repo_and_leaves_manual_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            one = make_repo(root, "one")
            two = make_repo(root, "two")
            auto_id = seed_jobs(one, auto=True)
            manual_id = seed_jobs(two, auto=False)
            for repo in (one, two):
                add_repo(repo, registry)

            log: list = []
            outcomes = hub_sweep(
                load_registry(registry),
                say=lambda _: None,
                process_batch_factory=recording_factory(log),
            )

            by_name = {item["name"]: item for item in outcomes}
            self.assertEqual(by_name["one"]["outcome"], "processed:1")
            self.assertEqual(by_name["two"]["outcome"], "idle")
            self.assertEqual(log, [("one", [auto_id], log[0][2], log[0][3])])
            config = load_config(repo=two)
            conn = connect(config.state.db)
            try:
                jobs = {job.id: job for job in list_jobs(conn)}
            finally:
                conn.close()
            self.assertEqual(jobs[manual_id].status, "queued")

    def test_sweep_skips_queueless_repos_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            fresh = make_repo(root, "fresh")
            add_repo(fresh, registry)

            outcomes = hub_sweep(
                load_registry(registry),
                say=lambda _: None,
                process_batch_factory=recording_factory([]),
            )

            self.assertEqual(outcomes[0]["outcome"], "skipped")
            self.assertFalse((fresh / ".mergetrain").exists())

    def test_sweep_isolates_broken_repos_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            gone = make_repo(root, "gone")
            live = make_repo(root, "live")
            seed_jobs(live, auto=True)
            add_repo(gone, registry)
            add_repo(live, registry)
            (gone / ".mergetrain.yaml").unlink()
            gone.rmdir()

            log: list = []
            outcomes = hub_sweep(
                load_registry(registry),
                say=lambda _: None,
                process_batch_factory=recording_factory(log),
            )

            self.assertEqual(outcomes[0]["outcome"], "error")
            self.assertIn("missing", outcomes[0]["error"])
            self.assertEqual(outcomes[1]["outcome"], "processed:1")
            self.assertEqual(len(log), 1)

    def test_serial_concurrency_never_overlaps_repo_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            for name in ("a", "b", "c"):
                repo = make_repo(root, name)
                seed_jobs(repo, auto=True)
                add_repo(repo, registry)

            log: list = []
            hub_sweep(
                load_registry(registry),
                concurrency=1,
                say=lambda _: None,
                process_batch_factory=recording_factory(log),
            )

            self.assertEqual(len(log), 3)
            spans = sorted((start, end) for _, _, start, end in log)
            for (_, first_end), (second_start, _) in zip(spans, spans[1:]):
                self.assertLessEqual(first_end, second_start)

    def test_parallel_concurrency_processes_every_repo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            for name in ("a", "b"):
                repo = make_repo(root, name)
                seed_jobs(repo, auto=True)
                add_repo(repo, registry)

            log: list = []
            outcomes = hub_sweep(
                load_registry(registry),
                concurrency=2,
                say=lambda _: None,
                process_batch_factory=recording_factory(log),
            )

            self.assertEqual([item["outcome"] for item in outcomes], ["processed:1"] * 2)
            self.assertEqual(len(log), 2)


class HubDaemonLoopTests(unittest.TestCase):
    def test_once_returns_final_sweep_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            repo = make_repo(root, "solo")
            seed_jobs(repo, auto=True)
            add_repo(repo, registry)

            outcomes = hub_daemon_loop(
                registry=str(registry),
                once=True,
                say=lambda _: None,
                install_signal_handlers=False,
                process_batch_factory=recording_factory([]),
            )

            self.assertEqual(outcomes[0]["name"], "solo")
            self.assertEqual(outcomes[0]["outcome"], "processed:1")

    def test_once_with_empty_registry_is_calm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            outcomes = hub_daemon_loop(
                registry=str(Path(td) / "absent.json"),
                once=True,
                say=lambda _: None,
                install_signal_handlers=False,
            )
            self.assertEqual(outcomes, [])


if __name__ == "__main__":
    unittest.main()
