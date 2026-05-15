#!/bin/bash
# Drop-priv shim for the bot + dashboard containers.
#
# Runs briefly as root to chown the bind-mounted state directories to the
# non-root `dca` uid, then exec's the real command under gosu. Idempotent
# — re-running on every container start is cheap and self-heals if a
# tenant directory was created with the wrong owner.
set -e

# The bot writes to /app/data (SQLite event log + secrets DB) and
# /app/reports (tax CSV exports). The dashboard also writes to /app/config
# (strategy YAML edits via the settings page). Daemon's /app/config mount
# is :ro so chown there is a no-op for the daemon container.
# `2>/dev/null || true` because read-only bind-mounts return EPERM on
# chown and we don't want a noisy log line.
chown -R dca:dca /app/data /app/reports 2>/dev/null || true
chown -R dca:dca /app/config 2>/dev/null || true

# Hardcode `bitcoiners-dca` here so the tenant compose's `command:`
# stanza can stay short (`["run", "--config", ...]`) the way it did
# before this shim existed. The old ENTRYPOINT was `["bitcoiners-dca"]`
# and Docker concatenated ENTRYPOINT+CMD; we're preserving that
# convention without forcing every caller to know the CLI binary name.
exec gosu dca:dca bitcoiners-dca "$@"
