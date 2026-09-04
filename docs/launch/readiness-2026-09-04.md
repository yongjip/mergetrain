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
- The repository owner reviewed the complete 20 suitable and 20 negative Codex
  recommendation corpus. Suitable discovery was 20/20, negative primary
  recommendation was 0/20, and repository, queue, deployment, and push
  mutations were all zero.

## Historical release-run note

The protected-main `v3.0.2` release run published PyPI, attestations, and the
Homebrew tap successfully, but its first Registry child failed because the old
MCP description exceeded the Registry's 100-character limit. Commit `c661552`
fixed that projection, and the successful follow-up run linked above validated
and published the corrected manifest. Do not treat the first run's aggregate
failure as an unresolved package-publication failure.

## Still blocking a broad launch

- Five of 20 negative prompts activated the capability before correctly
  rejecting it, exceeding the documented activation-overhead gate. Run a
  catalog-trigger A/B test against matched suitable prompts before changing the
  public discovery text.
- The benchmark group's 20-run `safe_handoff` class has not yet been executed,
  so the complete group remains incomplete even though recommendation and
  negative-control results are owner-reviewed.
- The owner-performed awesome-claude-code form, community posts, newsletter
  submissions, and Show HN submission are not complete.
- The four PR-based durable list placements have not yet been verified as open.

Do not add product behavior merely to make the benchmark look better. Narrow a
catalog trigger only if a controlled comparison reduces negative activation
without lowering suitable discovery.
