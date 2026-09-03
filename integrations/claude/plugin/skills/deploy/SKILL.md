---
name: deploy
description: >-
  Human-invoked workflow to validate the combined coding-agent train and deploy
  one exact Git plan through attributable confirmation. Never invoke from
  inferred intent.
disable-model-invocation: true
---

# Deploy one exact plan

1. Call `mergetrain_status` and read its health, state, and next action.
2. Invoke `mergetrain_deploy` without selection or approval arguments. It may
   validate queued work, then presents the exact tasks, destination refs, and
   gate policy through the client's human confirmation dialog. Train IDs and
   plan hashes remain internal.
3. Do not substitute a shell deploy command or a model-supplied confirmation.
   If the tool returns `confirmation_required`, show its terminal command and
   stop. If it returns `deploy_not_confirmed`, report that nothing was pushed.
4. Report `result`, deployed job IDs, `deploy_sha`, `push_status`, and
   `verify_status`.
