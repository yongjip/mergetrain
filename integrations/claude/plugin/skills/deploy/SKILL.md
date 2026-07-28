---
name: deploy
description: Deploy one validated mergetrain train through an attributable human confirmation dialog.
argument-hint: "[train-id]"
disable-model-invocation: true
---

# Deploy a validated train

1. Call `mergetrain_doctor` and `mergetrain_status` again.
2. If `$ARGUMENTS` names a train, verify that exact `train_id` is still
   deploy-eligible. If it is empty and several trains are pending, stop and ask
   the user to select one.
3. Invoke `mergetrain_deploy` with the selected `train_id`. This tool presents
   the job IDs, branches, recorded HEADs, integration ref, next action, and
   blocked or failed work in a client-rendered confirmation dialog.
4. Do not substitute a shell deploy command or a model-supplied confirmation.
   If the tool returns `confirmation_required`, show its terminal command and
   stop. If it returns `deploy_not_confirmed`, report that nothing was pushed.
5. Report `result`, deployed job IDs, `deploy_sha`, `push_status`, and
   `verify_status`.
