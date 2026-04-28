#!/bin/bash
# >>> fetch-runner-guard:BEGIN user=deploy
if [ "$(whoami)" != "deploy" ] || [ "$(id -u)" -eq 0 ]; then
    printf 'fetch-runner-guard: refusing to run as %s (uid %s); required: deploy, non-root\n' "$(whoami)" "$(id -u)" >&2
    exit 1
fi
# <<< fetch-runner-guard:END

set -euo pipefail

# This script is designed to run identically whether fetch-runner invoked it
# on a new commit or a human invoked it from a terminal. The guard above is
# the only invariant: the caller must be user=deploy and not root.

cd "$(dirname -- "$0")"

echo "deploying $(basename -- "$PWD")"

# Your deploy steps here, e.g. a docker compose rollout:
#   docker compose pull
#   docker compose up --detach --remove-orphans
#   docker image prune --force
