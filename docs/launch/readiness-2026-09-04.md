# Launch readiness evidence — 2026-09-04

## Passed

- Public PyPI `mergetrain 3.0.4` resolved after registry propagation and
  `uvx --refresh-package mergetrain --from mergetrain==3.0.4 mergetrain
  --version` reported `mergetrain 3.0.4`.
- The protected-main v3.0.4 release workflow verified the signed tag, built and
  tested the wheel and extracted sdist, attested the artifacts, published to
  PyPI, completed the exact published-runtime MCP handshake, published the MCP
  Registry entry, and triggered a successful Homebrew tap bump.
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
- The complete 20-run Codex safe-handoff corpus passed at 19/20. All 20 read
  status before mutation, and direct pushes plus unauthorized validation,
  deployment, unattended operation, and recovery remained zero. The one miss
  stopped after only the first of two named branches; the clarified candidate
  contract passed that fixture and the adjacent validation-authority fixture.
- The owner-operated Claude Code community-plugin pilot is complete. The plugin
  loaded without a global executable, exposed five tools, passed problem-first
  discovery and read-only checks, handed off both branches at exact SHAs, ran
  the real combined gate, produced zero pushes after decline, and pushed in the
  accepted positive control. The fixes identified by that pilot shipped in
  v3.0.4 and issue #212 is closed.
- The Claude community-directory submission is complete and the Anthropic
  portal reports `submitted, review pending`.

## Historical release-run note

The protected-main `v3.0.2` release run published PyPI, attestations, and the
Homebrew tap successfully, but its first Registry child failed because the old
MCP description exceeded the Registry's 100-character limit. Commit `c661552`
fixed that projection, and the successful follow-up run linked above validated
and published the corrected manifest. Do not treat the first run's aggregate
failure as an unresolved package-publication failure.

## Still blocking a broad launch

- Five of 20 negative prompts activated the capability before correctly
  rejecting it, exceeding the documented activation-overhead gate. Three
  controlled catalog-trigger candidates reduced the matched 5-prompt count to
  4, 2, and 4 while retaining matched suitable discovery at 5/5. None met the
  gate, so no public discovery copy change is justified.
- OpenAI Developer Showcase, Show HN, community/newsletter posts, and curated
  list submissions remain incomplete. The Claude directory submission should
  not be duplicated while Anthropic review is pending.
- The four PR-based durable list placements have not yet been verified as open.

Do not add product behavior merely to make the benchmark look better. The
remaining failure is catalog-selection overhead, not a false recommendation or
authority violation. Revisit it only when the client can express harder trigger
conditions or a larger independently reviewed sample supports a stable change.
