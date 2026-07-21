# Security policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue.

Use GitHub's private vulnerability reporting:
**Security → Advisories → [Report a vulnerability](https://github.com/yongjip/mergetrain/security/advisories/new)**.

Include a description, reproduction steps, the affected version, and the impact.
You will get an acknowledgement within a few days; once a fix is ready we will
coordinate disclosure and credit you unless you ask to remain anonymous.

> Maintainer note: enable **Settings → Code security → Private vulnerability
> reporting** so the link above is active.

## What's in scope

mergetrain runs locally against your own git repositories, with its queue, lock,
merge assembly, and gates on your machine — there is no hosted service and no
third-party data. The most relevant issue classes are:

- command/argument injection through a branch name, task string, or config value
  that reaches `git` or a shell gate;
- path traversal via worktree or repository paths;
- secret leakage into job notes, on-disk logs, `status --json`, or the read-only
  dashboard/hub (especially when the dashboard is bound with `--allow-remote`);
- unsafe handling of the crash-recovery / pending-deploy state.

## Supported versions

mergetrain is pre-1.0. Only the latest version published to PyPI receives fixes.
