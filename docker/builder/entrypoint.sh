#!/usr/bin/env bash
# Puts emsdk (emcc, node) on PATH, then runs the command. Files written into the
# mounted repository are handed back to the host user (HOST_UID/HOST_GID) at the end.
set -uo pipefail
# shellcheck disable=SC1091
. "$EMSDK/emsdk_env.sh" >/dev/null 2>&1

"$@"
rc=$?
if [[ -n "${HOST_UID:-}" && "$(id -u)" == 0 ]]; then
  for d in ${CHOWN_PATHS:-/src/build /src/orig /src/install}; do
    [[ -e "$d" ]] && chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" "$d"
  done
fi
exit $rc
