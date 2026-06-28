# Adapter pattern

Core trainyard is provider-neutral. Service-specific behavior should live in a
thin adapter or wrapper.

A wrapper may:

- Inject `--repo`, `--config`, and `--db` defaults.
- Translate legacy command names to trainyard commands.
- Set service-specific environment variables.
- Provide service-specific gate and verify scripts.

A wrapper should not:

- Reimplement queue state.
- Push deploy refs directly.
- Decide unattended deployment without explicit approval.
- Store provider credentials in repository config.

See `integrations/generic/scripts/local_ci.py` for a minimal wrapper shape.
