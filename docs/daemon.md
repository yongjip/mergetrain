# Daemon

`trainyard daemon` is a foreground auto-only worker.

```sh
trainyard daemon --interval 15
trainyard daemon --once
```

## Behavior

- Claims only `queued` jobs with `auto_deploy = 1`.
- Leaves manual queued jobs untouched.
- Uses the same runner lock as manual runners.
- Catches tick exceptions, logs them to stdout/stderr, and attempts an
  owner-guarded lock release.
- Handles SIGINT/SIGTERM by finishing the current tick before exiting.

## Recommended usage

For a simple local service:

```sh
nohup trainyard daemon --interval 15 >> .trainyard/daemon.log 2>&1 &
```

For schedulers, prefer one-shot ticks:

```sh
trainyard daemon --once
```

Then run it from cron, launchd, systemd timer, or a service-specific supervisor.

## Safety boundary

The daemon does not decide whether a job is safe for unattended deploy. It only
trusts the enqueue-time `--auto` flag. Your wrapper, agent instruction, or human
operator must enforce explicit approval before `--auto` is used.
