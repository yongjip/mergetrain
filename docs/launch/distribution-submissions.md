# External distribution packet

This packet keeps public submissions consistent without turning distribution
copy into another product contract. Verify links and current release state
before pasting it into a third-party form.

## Positioning

**One line**

Merge queue for committed branches from parallel coding-agent worktrees.

**Short description**

mergetrain lets coding agents finish in parallel without making the operator
the merge coordinator. Agents commit and enqueue their worktree branches; one
local runner assembles them in order, tests the combined tree, and performs one
approval-gated atomic Git push with durable recovery evidence.

**Audience**

Developers running multiple coding agents in Git worktrees who integrate local
branches directly or before opening a pull request.

**Not for**

Single-agent work, hosted review workflows, or teams already served by a
forge-native pull-request merge queue.

## Canonical links

- Landing page: <https://yongjip.github.io/mergetrain/>
- Repository: <https://github.com/yongjip/mergetrain>
- Demo: `uvx mergetrain demo`
- PyPI: <https://pypi.org/project/mergetrain/>
- Documentation: <https://github.com/yongjip/mergetrain/tree/main/docs>
- Security boundary: <https://github.com/yongjip/mergetrain/blob/main/docs/security.md>
- Claude plugin source: <https://github.com/yongjip/mergetrain/tree/main/integrations/claude/plugin>
- Codex plugin source: <https://github.com/yongjip/mergetrain/tree/main/plugins/mergetrain>
- MCP Registry name: `io.github.yongjip/mergetrain`

## Claude community marketplace submission

The public submission form adds reviewed third-party plugins to
`claude-community`, not to the separately curated
`claude-plugins-official` marketplace. The form records a repository URL and
optional subdirectory; it has no separate version or commit field. Keep the
default branch's release pins current, run strict validation, and test the
release-pinned MCP server immediately before submission or review follow-up.

Current status (2026-09-04): submitted; Anthropic review pending. Do not create
a duplicate submission. Respond through the existing submission if Anthropic
requests changes.

- Plugin name: `mergetrain`
- Display name: `mergetrain`
- Category: Development tools
- Source repository: `https://github.com/yongjip/mergetrain`
- Source path: `integrations/claude/plugin`
- License: MIT
- Homepage: `https://yongjip.github.io/mergetrain/`
- Contact/author: Yongjip Kim / `https://github.com/yongjip`
- Description: use the short description above
- Security note: The plugin launches the release-pinned PyPI MCP extra through
  `uvx`. Queue state remains local. Deploy and recovery actions remain
  approval-gated, and configured Git, gate, and verification commands are the
  only external execution boundary.

Submission routes:

- Individual author: <https://platform.claude.com/plugins/submit>
- Team/Enterprise directory manager:
  <https://claude.ai/admin-settings/directory/submissions/plugins/new>

## OpenAI Developer Showcase submission

- Project name: mergetrain
- Headline: Safely integrate branches from parallel Codex worktrees
- Category: Codex / Agents / Developer tools
- Project URL: `https://yongjip.github.io/mergetrain/`
- Source URL: `https://github.com/yongjip/mergetrain`
- Demo command: `uvx mergetrain demo`

**Submission description**

I run several coding agents in parallel Git worktrees. They finish in parallel,
but integrating their branches made me the bottleneck. mergetrain is the local
queue for that last mile: agents commit and enqueue; one runner assembles the
exact commits, tests them together, asks for approval, and atomically updates
the destination with recovery evidence. A native Codex plugin teaches agents
the narrow handoff boundary, and a release-pinned MCP adapter exposes five
approval-aware tools. The repository includes reproducible discovery,
safe-handoff, semantic-conflict, and killed-push recovery evidence.

**What Codex contributed**

Codex was used both to develop the project and as the subject of fixed-corpus
behavioral evaluations. The latest owner-operated evidence recorded 20/20
appropriate discovery and 19/20 safe handoff, with zero direct pushes or
unauthorized actions. These are transparent project evaluations rather than
external-user adoption claims.

## Launch copy

### Show HN title

Show HN: mergetrain – A local merge queue for parallel coding agents

### Short community post

I run several coding agents in parallel worktrees. They finish in parallel, but
merging them made me the bottleneck. I built mergetrain for that last mile:
agents commit and enqueue, one local runner tests the combined tree, and one
explicitly approved atomic push lands it. It is open source, has no hosted
control plane or product telemetry, and the complete workflow runs in a
disposable demo: `uvx mergetrain demo`.

### Curated-list entry

`mergetrain` — Local merge queue that assembles and jointly tests committed
branches from parallel coding-agent worktrees before one approval-gated atomic
Git push.

## Measurement without a tracking server

Record a weekly UTC snapshot for the first four weeks after launch:

- landing-page deployment and uptime;
- GitHub unique visitors, referring sites, clones, stars, and first-time
  contributors;
- PyPI downloads by version from a public aggregate such as pypistats.org;
- external issues that report an install, discovery, or integration outcome;
- Claude community catalog status and MCP Registry latest-version status; and
- the number of independently operated repositories completing a first
  enqueue, validated train, and deploy, collected only from voluntary reports.

Do not add product telemetry or a hosted mergetrain account to obtain these
numbers. Distinguish owner-run evaluations, external-repository evidence, and
external-user evidence in every public claim.
