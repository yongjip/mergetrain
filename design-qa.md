# Dashboard Design QA

## Evidence

- Source visual truth: `/Users/yongjip/.codex/generated_images/019f6ab0-17e0-7e43-a4f9-4f080fd7505e/exec-b523bd1e-b6b9-4b57-9a89-ee9d5b8e7968.png`
- Browser-rendered implementation: `/private/tmp/mergetrain-dashboard-final.png`
- Full-view comparison: `/private/tmp/mergetrain-dashboard-comparison-final.png`
- Focused main-track comparison: `/private/tmp/mergetrain-dashboard-focused-main.png`
- Focused status-rail comparison: `/private/tmp/mergetrain-dashboard-focused-rail.png`
- Responsive capture: `/private/tmp/mergetrain-dashboard-responsive.png`
- URL: `http://127.0.0.1:8765/`
- Viewport: requested `1440 × 1024` CSS pixels; rendered client width `1425` because of the vertical scrollbar. Responsive check used `820 × 1180`.
- State: three assembled jobs, train-wide gate 2 running, healthy local runner, one blocked historical job, live SSE connection.

## Findings

- No actionable P0, P1, or P2 mismatch remains.
- [P3] Dynamic task names wrap to two lines in the implementation where the reference uses shorter single-line examples. The fixed two-line title area keeps real job cards aligned and prevents truncating useful task context, so this is accepted as resilient behavior.
- [P3] The implementation represents the dynamic gate set with a count badge instead of four permanently rendered substep circles. This preserves the reference hierarchy without implying a fixed number of gates.

## Required Fidelity Surfaces

- Fonts and typography: bundled Inter Variable and JetBrains Mono Variable reproduce the reference's sans/mono hierarchy, weights, compact timestamps, and code labels. Long task and branch values wrap or truncate without changing card alignment.
- Spacing and layout rhythm: the desktop frame keeps the header, main train track, activity timeline, and persistent right rail in the same visual order and proportion as the reference. Card spacing, dividers, border radii, and vertical rhythm are consistent. At 820 px the rail moves below the main content and job cards stack without horizontal overflow (`scrollWidth == clientWidth == 805`).
- Colors and visual tokens: blue progress, green success/health, amber next action, red blocked state, navy text, warm-white background, and low-contrast dividers map directly to the reference semantics and retain readable contrast.
- Image quality and asset fidelity: this dashboard has no photographic or illustrative imagery. Visible icons and the favicon use the bundled Phosphor icon family; no emoji, placeholder bitmap, handcrafted inline SVG, or CSS-drawn substitute is used.
- Copy and content: headings, status messages, blocked reason, next safe action, and read-only footer are understandable without external context. Dynamic differences in job IDs, task names, and timestamps are expected test data, not design drift.
- Icons and polish: icons share one stroke/fill family, align to text baselines, and change color consistently with state. The motion indicator stops under `prefers-reduced-motion: reduce`.
- Accessibility: document landmarks, heading levels, named train/job regions, semantic time/code content, responsive text wrapping, and reduced-motion handling are present. The dashboard intentionally has no controls or mutation actions, so there are no keyboard-only interactions to exercise.

## Browser and Functional Checks

- Primary behavior: initial snapshot render, live SSE state, relative time updates, train/job progress, activity timeline, blocked history, next-safe-action explanation, and responsive recomposition.
- Read-only contract: mutation methods return `405 read_only` in the automated server test.
- Console: 0 errors and 0 warnings after the final navigation.
- Network: `/api/snapshot` returned 200 and the page reported `LIVE`.
- Desktop overflow: none (`scrollWidth == clientWidth == 1425`).
- Responsive overflow at 820 px: none (`scrollWidth == clientWidth == 805`).
- Focused comparisons were used because card typography, status semantics, runner health, blocked detail, and next-action copy were too small to judge reliably from the combined full view alone.

## Comparison History

1. Initial comparison found P2 font loading and asset issues: the inlined mono subset was blocked by the CSP and the favicon returned 404. The build now emits self-hosted font files, retains `font-src 'self'`, and generates a Phosphor-based favicon. The post-fix desktop capture renders without font artifacts or console errors.
2. The next comparison found a P2 state-semantics issue: jobs already merged into the train could still appear waiting during train-wide gates. Runner events now record each job's assembly result, the snapshot exposes completed job IDs, and cards correctly show assembly complete while the gate phase remains train-wide. Activity suppresses redundant per-job merge noise when a batch-level assembly event exists.
3. The accessibility pass found a P2 continuous-motion gap. A reduced-motion media query now disables the live spinner animation. Browser inspection confirmed the rule is present in the loaded production stylesheet.
4. Final full-view and focused comparisons found no remaining actionable P0/P1/P2 issue. The two P3 differences above are intentional data-resilience choices.

## Implementation Checklist

- [x] Match the selected Live Track / Signal Board direction.
- [x] Preserve truthful train-wide and per-job status semantics.
- [x] Bundle fonts and icon assets under the local CSP.
- [x] Verify live desktop rendering with zero console errors.
- [x] Verify responsive stacking and zero horizontal overflow.
- [x] Support reduced motion.
- [x] Keep every dashboard surface read-only.

## Follow-up Polish

- If a future compact mode is added, allow users to choose one-line task titles; keep the current two-line default for operational clarity.

final result: passed
