# Launch readiness evidence — 2026-09-04

## Passed

- Public PyPI `mergetrain 3.0.2` installed from a fresh `uv` cache on macOS.
  `mergetrain --version` reported `3.0.2`, and
  `mergetrain status --diagnose --json` reported a healthy wheel install.
- The public `mergetrain demo` completed all nine stages against a disposable
  repository: four exact-SHA enqueues, combined-only failure isolation, two
  compatible jobs validated together, explicit deploy, atomic local-remote
  update, and successful verification.
- The follow-up Ubuntu MCP Registry run installed the pinned Registry runtime,
  launched the exact published stdio server, completed its handshake, and
  published successfully: [run 33823985776](https://github.com/yongjip/mergetrain/actions/runs/33823985776).
- GitHub rendered `docs/images/demo.gif` at a 390 × 844 mobile viewport without
  horizontal overflow. The 1200 × 720 source rendered at 324 × 194.4 inside the
  324px README column; document scroll width remained 390px. The source asset is
  704KB.
- README comparison sections already distinguish plain worktrees, hosted merge
  queues, and mergetrain. The README now states the no-account, no-hosted-control-
  plane, no-OAuth, and no-product-telemetry boundary explicitly.
- The 60-second demo and dashboard work tracked by issues 171 and 173 is closed.
  Agent-native packaging tracked by issue 172 is also closed.

## Historical release-run note

The protected-main `v3.0.2` release run published PyPI, attestations, and the
Homebrew tap successfully, but its first Registry child failed because the old
MCP description exceeded the Registry's 100-character limit. Commit `c661552`
fixed that projection, and the successful follow-up run linked above validated
and published the corrected manifest. Do not treat the first run's aggregate
failure as an unresolved package-publication failure.

## Still blocking a broad launch

- An owner must review the completed 20 suitable and 20 negative Codex
  transcripts before discovery rates are claimed. The unscored triage currently
  identifies five possible negative activations but zero negative primary
  recommendations and zero mutations.
- The owner-performed awesome-claude-code form, community posts, newsletter
  submissions, and Show HN submission are not complete.
- The four PR-based durable list placements have not yet been verified as open.

Do not add product behavior merely to make the pending benchmark look better.
Confirm the observations first; then narrow catalog triggers only if the
activation finding survives independent review.
