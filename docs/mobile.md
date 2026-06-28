# Manage mergetrain from your phone

mergetrain is a local CLI: the queue, the git worktrees, and the deploy all run on your machine. The cleanest way to drive it from a phone is **Cowork Dispatch** — you message a task from the Claude mobile app, and it runs on your Mac/PC Desktop, where it can run `mergetrain` against your repo and message you the result. No server to host, no SSH tunnels.

This repo ships two things that make that work well:

- [`CLAUDE.md`](../CLAUDE.md) — tells the on-machine agent how to operate the queue safely (read state first, validate freely, **confirm before deploy**).
- `scripts/ty-status.sh`, `scripts/ty-validate.sh`, `scripts/ty-deploy.sh` — phone-friendly wrappers with a deploy guard.

> Prefer a terminal? You can also drive a running session with **Remote Control** (`claude remote-control`) or just SSH in from a phone terminal app — see [Alternatives](#alternatives).

## Requirements

- A **Pro or Max** plan (Dispatch is a Cowork research preview).
- The latest **Claude Desktop** app installed and **running**, on a computer that **stays awake**.
- The latest **Claude mobile** app, signed in with the **same account**.
- Internet on both devices.
- `mergetrain` installed on the machine (`pipx install /path/to/mergetrain`) and a repo with `.mergetrain.yaml`.

## One-time setup

1. Download or update **Claude Desktop** (macOS/Windows) and **Claude mobile** (iOS/Android).
2. Open **Cowork** on either device.
3. Click **Dispatch** in the left side panel → **Get started**.
4. On the setup screen, toggle on **file access** and **keep your computer awake**, then **Finish setup**.
5. Start messaging Claude inside the **Dispatch** section.

That's it — the same conversation now syncs between phone and desktop. Full details: [Assign tasks to Claude from anywhere in Cowork](https://support.claude.com/en/articles/13947068) and the [Dispatch / Remote Control overview](https://code.claude.com/docs/en/remote-control).

## How it works with mergetrain

You send a short message from your phone. On your Mac, Claude reads [`CLAUDE.md`](../CLAUDE.md), runs the right `mergetrain` commands in your repo, and replies with a short summary (plus a push notification when it's done or needs your go-ahead).

**Deploy policy: confirm, then deploy.** Status checks and `--validate-only` run freely. For an actual deploy, the agent first summarizes exactly what will ship and waits for your explicit "deploy / yes / go" in the thread before running `run-batch --deploy`. The `scripts/ty-deploy.sh` wrapper enforces the same thing on the command line: it prints what would ship and does nothing unless you pass `--confirm`.

## Phone phrasebook

Type these to the Dispatch thread. Both languages work — say it however is natural.

| Intent | 한국어 예시 | English example |
|---|---|---|
| Check the queue | "mergetrain 큐 상태 알려줘" | "What's in the mergetrain queue?" |
| Validate the train | "큐에 있는 거 validate 돌려줘" | "Validate the queued train" |
| Deploy (guarded) | "검증 통과했으면 배포 준비해줘" | "If it validated, get ready to deploy" |
| Confirm the deploy | "응, 배포해" | "Yes, deploy it" |
| Triage a blocker | "blocked 있으면 원인만 알려줘" | "If anything's blocked, just tell me why" |
| Preview cleanup | "임시 worktree 정리할 거 있는지 봐줘" | "Any temp worktrees to clean up?" |

The agent will run the matching commands (e.g. `scripts/ty-status.sh`, `mergetrain run-batch --validate-only`) and, for deploy, will show you the plan and wait for your confirmation first.

## Keeping the machine awake

Dispatch setup includes a "keep your computer awake" toggle — turn it on. As a backup on macOS you can also run, in a terminal you leave open:

```sh
caffeinate -dimsu
```

If the Desktop app is closed or the machine sleeps, Dispatch can't run your tasks.

## Safety

Dispatch gives your phone a path to real actions on your computer — including running deploys, and reading/moving/deleting files. mergetrain's design helps (every deploy needs an explicit `--deploy`, and `CLAUDE.md` requires confirmation first), but treat it seriously:

- Keep deploy **manual-confirm** (the default here). Don't switch to `--auto`/`daemon` for remote use unless you fully trust unattended deploys.
- Know how to stop it fast: quit the Claude Desktop app, or turn off file access in the Dispatch setup screen.
- Be cautious with instructions or links that originate from outside you. See [Use Cowork safely](https://support.claude.com/en/articles/13947068).

## Alternatives

- **Remote Control** — run `claude remote-control` in your repo, then drive that live session from the Claude app or `claude.ai/code`. Best when you want to steer an in-progress terminal session rather than fire off a task. See the [docs](https://code.claude.com/docs/en/remote-control).
- **SSH from a phone terminal** (Termius, Blink, Termux) — since every command is JSON-first and non-interactive, the wrappers in `scripts/` work well over SSH. Pair with Tailscale for secure access.

> Note: **Claude Code on the web** runs in Anthropic's cloud, not on your machine, so it can't see your local mergetrain queue/SQLite — it's the wrong tool for managing a local deploy train.
