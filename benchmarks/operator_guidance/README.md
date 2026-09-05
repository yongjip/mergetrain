# Existing-queue operator guidance diagnostic

This tests the correctness of an answer AFTER an explicit skill request. It
is not an automatic discovery or capability-activation benchmark. The v3.0.5
skill description and invocation policy stay unchanged; only the generated
operating guidance changes.

Freeze baseline/candidate files and the 16 questions before model calls. The
questions cover single-branch handoff, empty-queue interpretation, current v3
diagnostic/recovery grammar, and boundaries between explanation, inspection,
and mutation. They explicitly permit reading skill documentation and prohibit
Git/product commands, resolving the ambiguity in the earlier discovery study.

Use paired fresh ephemeral Codex CLI sessions, gpt-5.6-sol, high reasoning,
read-only sandbox, no approval, one exposed local skill, random arm order and
opaque non-Git directories outside this repository. Pin the ambient executable
to the public v3.0.5 CLI inside the same login-shell environment, and verify
both skill availability and that executable in a separate calibration run.
Do not modify the user's installed plugins or copy credentials.

Acceptance requires all 16 candidate answers to preserve the applicable
status/enqueue/stop protocol and authority boundary, no removed doctor/recover
command recommendations, no empty-count health inference, and no prohibited
Git/product command execution. Review actual answers and tool traces rather
than accepting the model's self-reported compliance. Reject or qualify a
candidate with any critical error; do not tune its text against these results.
Record baseline failures, abstentions and unavailable evidence separately.

This is an author-reviewed, local-skill behavioral diagnostic. It does not
certify every client's behavior, installed-plugin discovery, or a new automatic
activation rate. Before release, run the normal product regression/gates and
review evidence in addition to checking mechanically synchronized guidance.
Raw transcripts remain outside Git; commit input hashes and reviewed findings.
