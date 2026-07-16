# Dashboard Design QA

## Evidence

- Source visual truth: `/Users/yongjip/.codex/generated_images/019f6ab0-17e0-7e43-a4f9-4f080fd7505e/exec-b523bd1e-b6b9-4b57-9a89-ee9d5b8e7968.png`
- User feedback source: `/var/folders/q_/dsp0jv093b56dl2zvd9dzjs00000gn/T/codex-clipboard-f7d7e0b8-6f1c-4b1c-ac07-6277e25d8c59.png`
- Browser-rendered implementation: `/private/tmp/mergetrain-dashboard-clarity-viewport.png`
- Full-view comparison: `/private/tmp/mergetrain-dashboard-clarity-comparison.png`
- Focused Activity comparison: `/private/tmp/mergetrain-dashboard-activity-comparison.png`
- Responsive capture: `/private/tmp/mergetrain-dashboard-clarity-responsive.png`
- URL: `http://127.0.0.1:8765/`
- Viewports: `1440 × 1024` desktop and `820 × 1180` responsive.
- State: PREVIEW DATA, three assembled jobs, gate 3/4 (`e2e`) running, `diff-check` and `unit` complete, `package` waiting, active runner, one blocked historical job, connected SSE stream.

## Findings

- No actionable P0, P1, or P2 issue remains.
- [P3] The added current-check panel makes the desktop page taller than the original visual target. This is accepted because the requested operational meaning, exact command, gate sequence, and scope now appear before the job cards without weakening the existing hierarchy.
- [P3] At 820 px, the long Activity list naturally extends below the fold. It remains readable and has no horizontal overflow; a future density preference could offer a compact timeline without removing the default explanations.

## Required Fidelity Surfaces

- Fonts and typography: bundled Inter Variable and JetBrains Mono Variable remain consistent with the source. The new hierarchy uses a 24 px current-check title, 14 px explanations, and 10–12 px phase/state metadata. Commands truncate safely rather than stretching the layout.
- Spacing and layout rhythm: the original header, train rail, job track, activity timeline, and right status rail remain in the same order and proportions. The current-check panel uses the existing 7 px radius, line weight, blue state token, and compact grid rhythm rather than introducing a different component language.
- Colors and visual tokens: blue running, green complete, amber preview/next action, red blocked, gray started/waiting, navy text, warm-white background, and low-contrast separators preserve the source semantics. `STARTED` is deliberately neutral so a historical start event cannot look currently active.
- Image quality and asset fidelity: there is no photographic or illustrative imagery. All visible icons remain from the bundled Phosphor family; there are no emoji, handcrafted SVGs, CSS illustrations, or placeholder assets.
- Copy and content: `CONNECTED` now names the browser data channel, `Runner ACTIVE/IDLE` names process ownership, PREVIEW DATA explicitly says that synthetic commands are not executing, and every Activity row states its phase, effective state, explanation, and command/detail where available.
- Accessibility and behavior: semantic landmarks and heading levels are intact; current gates use a named region and ordered list; preview data uses a status region; reduced motion remains supported. The dashboard has no controls or mutation actions, so there are no keyboard-only control paths to test.

## Browser and Functional Checks

- Primary behavior: initial snapshot, SSE-connected status, separate runner state, current gate summary, four-gate sequence, gate command, scope, elapsed time, Activity explanations, historical `STARTED` resolution, blocked history, and next-safe-action guidance.
- Desktop: `window.innerWidth = 1440`, `clientWidth = scrollWidth = 1425`; no horizontal overflow.
- Responsive: `window.innerWidth = 820`, `clientWidth = scrollWidth = 805`; no horizontal overflow.
- Console: 0 warnings and 0 errors after the final desktop reload.
- Read-only server behavior and preview labeling are covered by automated HTTP tests.
- Focused comparison was required because the phase/state chips, explanatory copy, and distinction between `RUNNING`, `COMPLETE`, and historical `STARTED` are too small to judge from the full-view comparison alone.

## Comparison History

1. The user feedback image exposed a P1 semantic ambiguity: `LIVE` described the SSE connection but looked like runner execution, and synthetic QA data looked operational. The header now says `CONNECTED`; Runner separately says `ACTIVE` or `IDLE`; `--preview` adds both a PREVIEW badge and a plain-language banner stating that no shown command is executing.
2. The same image exposed a P1 gate identity mismatch: the phase badge showed four gates while Activity said `2/3`. The snapshot now derives one shared gate total, structured names, indexes, states, and current gate from runner events. The final evidence consistently shows `Gate 3 of 4 · e2e` and the ordered `diff-check → unit → e2e → package` sequence.
3. The first clarity implementation still showed an old `Assembling validated train` start event as `RUNNING` after a later assembly success. This was a P1 truthfulness defect. Activity now resolves historical active events against later terminal events and labels them `STARTED`; only the unresolved latest gate remains `RUNNING`.
4. The initial Activity rows had a P2 comprehension gap: messages and raw details did not explain purpose or distinguish commands from context. Each row now has phase/state metadata, purpose copy, and a command treatment only for gates; non-command context is labeled `DETAIL`.
5. Final full-view, focused Activity, and responsive comparisons found no remaining actionable P0/P1/P2 issue.

## Implementation Checklist

- [x] Separate browser connectivity from runner execution.
- [x] Mark synthetic data unambiguously.
- [x] Show the exact current gate, position, purpose, command, scope, and elapsed time.
- [x] Show the complete ordered gate set and per-gate state.
- [x] Explain every visible Activity milestone.
- [x] Resolve historical starts so they do not look currently active.
- [x] Preserve the local read-only contract and responsive layout.
- [x] Verify zero browser console errors and zero horizontal overflow.

## Follow-up Polish

- Consider an optional compact Activity density after real-world use confirms whether operators prefer five detailed rows or more terse history.

final result: passed
