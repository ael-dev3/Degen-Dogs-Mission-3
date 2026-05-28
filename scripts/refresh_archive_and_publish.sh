#!/usr/bin/env bash
set -Eeuo pipefail

# Bridge runner: refresh the Mission 3 archive first, then run the normal cached
# dashboard publish flow with archive public JSON included in the commit.

export DEGEN_DOGS_RUN_MISSION3_ARCHIVE="${DEGEN_DOGS_RUN_MISSION3_ARCHIVE:-1}"
exec bash "$(dirname "${BASH_SOURCE[0]}")/refresh_and_publish.sh" "$@"
