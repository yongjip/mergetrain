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
   only the selected change set in human terms: task intent, branches and
   recorded HEADs, destination refs, gates, post-push verification, validation
   evidence, stale-base reassembly risk, next action, and all blocked, failed,
   or reconcile-pending work. The opaque train ID remains an internal execution
   binding, not the reason a human should approve the deploy.
4. Do not substitute a shell deploy command or a model-supplied confirmation.
   If the tool returns `confirmation_required`, show its terminal command and
   stop. If it returns `deploy_not_confirmed`, report that nothing was pushed.
5. Report `result`, deployed job IDs, `deploy_sha`, `push_status`, and
   `verify_status`.
