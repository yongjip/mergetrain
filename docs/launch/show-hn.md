# Show HN launch packet

Use this only after the [launch-readiness checks](readiness-2026-09-04.md) are
complete. Do not ask for votes or cross-post the same promotional text to
Reddit.

## Title

Show HN: Mergetrain – a local merge queue for parallel AI coding agents

## First comment

I built mergetrain after finding that parallel coding agents moved the
bottleneck rather than removing it. Worktrees let several agents edit at once,
but I was still manually deciding landing order, rebuilding combined trees,
rerunning tests, and making sure only one session pushed.

Mergetrain is the small serial integration spine I wanted: agents commit and
enqueue exact revisions, one local runner assembles them in order, the combined
tree passes configured gates, and deployment remains a separate human-approved
atomic Git update with post-push verification. If a push is interrupted, it
reconciles local evidence with the remote rather than guessing or replaying it.

The shortest way to see the workflow is:

```sh
uvx mergetrain demo
```

The demo creates a disposable repository and local bare remote. It exercises
four real branches, a failure that appears only in combination, conflict
attribution, and deployment of the compatible train. It does not touch one of
your repositories.

There is no account, hosted control plane, OAuth app, or product telemetry.
Queue and runner state stay local. Your configured Git remote and your own gate
or verification commands can of course use external services.

This is intentionally not a replacement for GitHub Merge Queue or GitLab Merge
Trains. Those are the better choice for PR-first teams. Mergetrain is for local,
worktree-first agent workflows where committed branches need a single ordered
handoff before or instead of a PR.

I would especially value feedback from people running multiple agents in one
repository: where does your integration bottleneck show up, and does this
boundary remove work or add ceremony?

## Same-day checklist

- Post Monday–Wednesday, 12:00–15:00 UTC.
- Add the first comment immediately after submission.
- Keep the day available to answer every top-level comment.
- Link the discussion, not an upvote request.
- If the submission gets no traction, use HN's second-chance email rather than
  reposting automatically.
