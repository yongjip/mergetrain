# Changelog

## Unreleased

- Preserve exact validated train identity for a later approval-gated deploy.
- Rebuild validated trains on the current integration ref and reject changed task HEADs.
- Exclude validated-but-not-deployed branches from destructive GC.
- Fence batch claims and state transitions with unique lease tokens.
- Heartbeat, cancel, and time out long-running Git and shell subprocesses.
- Reject explicitly empty deploy refs and invalid queue timing at config load.
- Return truthful JSON outcomes and non-zero exit codes for blocked/failed jobs.
- Version SQLite migrations with `PRAGMA user_version`.
- Add a loopback-first, read-only live dashboard with SSE and polling fallback.
- Distinguish browser connectivity from runner activity and explain the current gate, command, scope, and Activity milestones.
- Record structured runner phases and explicit lock heartbeat timestamps.
- Redact lease tokens and local filesystem paths from dashboard payloads.

## 0.1.0

Initial public package scaffold.

- SQLite-backed local deploy queue.
- Runner lock with PID liveness checks.
- Git worktree merge train execution.
- Configurable pre-push gates and post-push verify hooks.
- Atomic push refs.
- Auto-only daemon boundary.
- JSON-first agent contract, status, doctor, and GC output.
