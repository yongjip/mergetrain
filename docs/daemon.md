# Daemon

`mergetrain daemon` is a foreground runner with two explicit modes. Its default
mode deploys auto-approved work; `--validate-only` continuously validates manual
work but stops at the approval boundary.

```sh
mergetrain daemon --interval 15
mergetrain daemon --once
mergetrain daemon --validate-only --interval 15
mergetrain daemon --validate-only --once
```

## Behavior

- Default mode claims only `queued` jobs with `auto_deploy = 1`, then runs the
  deploy path exactly as before. It leaves manual queued jobs untouched.
- `--validate-only` claims only manual jobs (`auto_deploy = 0`) and invokes the
  batch runner with `deploy=False`; it cannot push or run post-push verify.
- Validation mode pauses before a writable claim whenever any `validated` row
  exists, including an incomplete legacy train identity. After a successful or
  partial validation creates a validated train, later ticks remain paused until
  that train is deployed, dismissed, or superseded.
- Both modes pause while deploy reconciliation is pending.
- Uses the same runner lock as manual runners.
- Catches tick exceptions, logs them to stdout/stderr, and attempts an
  owner-guarded lock release.
- Handles SIGINT/SIGTERM by finishing the current tick before exiting.

`--validate-only` cannot be combined with `--notify`. Existing headless
transitions describe deploy outcomes, and validation does not invent a second
notification contract. `hub daemon` remains auto-deploy-only.

## Recommended usage

For a simple local service:

```sh
nohup mergetrain daemon --interval 15 >> .mergetrain/daemon.log 2>&1 &
```

For schedulers, prefer one-shot ticks:

```sh
mergetrain daemon --once
mergetrain daemon --validate-only --once
```

Then run it from cron, launchd, systemd timer, or a service-specific supervisor.

## Safety boundary

The default daemon does not decide whether a job is safe for unattended deploy.
It only trusts the enqueue-time `--auto` flag. Your wrapper, agent instruction,
or human operator must enforce explicit approval before `--auto` is used.

Starting `daemon --validate-only` is explicit authorization to spend local
runner resources on merge and gates; it is not deploy approval. The pending
validated train still requires the normal exact-train approval and
`run-batch --deploy`.
