# Adapter pattern

Core mergetrain is provider-neutral. Service-specific behavior should live in a
thin adapter or wrapper.

A wrapper may:

- Inject `--repo`, `--config`, and `--db` defaults.
- Translate legacy command names to mergetrain commands.
- Set service-specific environment variables.
- Provide service-specific gate and verify scripts.
- Present the canonical `deploy` action consistently while preserving
  `status=deployed` and `deploy_sha` in machine-facing integrations.

A wrapper should not:

- Reimplement queue state.
- Push configured Git refs directly.
- Decide unattended deployment without explicit approval.
- Store provider credentials in repository config.

Core completion means the configured refs were atomically updated. An adapter
may perform provider verification or release after that push, but it must label
and authorize those provider actions separately. Do not reinterpret the stable
machine status `deployed` as proof that an App Store, cluster, or other provider
release completed.

See `integrations/generic/scripts/local_ci.py` for a minimal wrapper shape.
