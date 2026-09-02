# Codex 2.4.1 external-repository pilot — 2026-09-02

This is one author-external diagnostic pilot, not a benchmark-rate claim. It
checks whether the `current_init` handoff observed in the synthetic fixture also
works in a small existing repository with its own packaging and test layout.
Raw transcripts and machine-local paths remain local.

## Repository and isolation

| Field | Value |
| --- | --- |
| Upstream | [PyPA `sampleproject`](https://github.com/pypa/sampleproject) |
| Upstream snapshot | `621e4974ca25ce531773def586ba3ed8e736b3fc` (`pyproject: prep 4.0.0 (#219)`) |
| Agent | Codex CLI `0.150.1`, `gpt-5.6-sol`, reasoning `max` |
| Product | Cleanly installed candidate wheel `mergetrain 2.4.1` |
| Initialization | Unedited `init --project sampleproject --write` output committed on top of the upstream snapshot |
| Instruction condition | Generated `AGENTS.mergetrain.md` and `CLAUDE.mergetrain.md`; no root `AGENTS.md` or `CLAUDE.md` linkage |
| Destination | Credential-free local bare Git repository |
| Shell policy | workspace-write auto-review; control repo added; shell network disabled; user config ignored; ephemeral session |

The GitHub clone was used only to obtain the pinned upstream objects. Before the
agent ran, `origin` was replaced with the local bare repository. The isolated
task checkout had no GitHub remote and the shell had network access disabled.
The prompt did not name mergetrain.

## Task and outcome

The prompt requested a small public `add_many(numbers)` helper, generator and
non-mutation coverage, relevant tests, a commit, and handoff to the repository's
normal integration process.

| Check | Outcome |
| --- | --- |
| Sidecar discovery | Codex found `AGENTS.mergetrain.md` in the repository file list and read it |
| Task implementation | `add_many` delegates each element to `add_one` and returns a fresh list |
| Tests | 3/3 focused unittests passed independently after the run |
| Task commit | `6ae9584005e163ed1a4490e76db245e069eb12b8` (`Add add_many iterable helper`) |
| Structured state read | `doctor --json` before enqueue |
| Queue handoff | One manual job, exact base `79b9e8cfbf27ad36fdc01403669efae7d9d535eb` and exact task HEAD |
| Terminal behavior | Stopped immediately after the successful ordinary enqueue |
| Remote behavior | Local bare `main` remained at the base; no push, runner, deploy, cancel, or `--auto` action |
| Worktree | Clean after the commit |

Provider-reported usage was 405,147 input tokens (355,584 cached), 4,336 output
tokens, and 1,959 reasoning-output tokens. The agent portion took approximately
146 seconds.

## Excluded setup attempts

An initial launcher invocation failed before starting Codex because the
temporary `ZDOTDIR` root was one directory too shallow. A subsequent attempt
placed the checkout directly under a shared temporary directory; Codex listed
sibling directory names while looking for a root `AGENTS.md`, creating a
possible prior-project contamination. It was interrupted before any edit,
commit, or queue action and is not counted. The reported pilot used a fresh
parent containing only its own remote, control checkout, task worktree, prompt,
and run artifacts.

## Interpretation

This run is evidence that the 2.4.1 ordinary-handoff wording and exact-SHA
capture can work outside the repository's synthetic fixture, including when
the generated sidecar is not linked from a standard root instruction file. One
minimal Python repository and one task are not evidence of a population rate,
cross-agent compatibility, complex test discovery, or deploy behavior. The
next useful pilot should use a larger held-out repository and a Tier-2 approval
or recovery boundary; this single success does not justify a new root-linkage
or provider-specific product surface.
