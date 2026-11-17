#!/usr/bin/env bash
# Run the native build under a private Xvfb display and capture the screen at given
# times: the bring-up's eyes when there is no desktop (CI, WSL without WSLg).
#
#   tools/native/screenshot.sh <out-prefix> <install-root> [seconds...]
#
# Writes <out-prefix>-<s>.png for each time and <out-prefix>.log (stderr+stdout).
# The player's EULA decision is passed through from the environment, never set here.
set -u
OUT="$1"; ROOT="$2"; shift 2
TIMES=("${@:-5 10 20 35}")
[[ $# -eq 0 ]] && TIMES=(5 10 20 35)
DISP=":${XVFB_DISPLAY:-77}"
mkdir -p "$(dirname "$OUT")"
Xvfb "$DISP" -screen 0 1600x1200x24 >/dev/null 2>&1 &
XV=$!
sleep 1
DISPLAY="$DISP" SDL_AUDIODRIVER="${SDL_AUDIODRIVER:-dummy}" \
  "$(dirname "$0")/../../build/native/aoe2" "$ROOT" > "$OUT.log" 2>&1 &
GAME=$!
# Optional clicks: AOE2_SHOT_CLICKS="12:747,165 18:400,300" (second:x,y in screen pixels)
declare -A CLICK=()
for c in ${AOE2_SHOT_CLICKS:-}; do CLICK[${c%%:*}]=${c#*:}; done
ALL=$(printf '%s\n' "${TIMES[@]}" "${!CLICK[@]}" | sort -n | uniq)
t=0
for s in $ALL; do
  sleep $((s - t)); t=$s
  if [[ -n "${CLICK[$s]:-}" ]]; then
    xy=${CLICK[$s]}
    DISPLAY="$DISP" xdotool mousemove "${xy%,*}" "${xy#*,}" sleep 0.2 click 1 && echo "clicked $xy at ${s}s"
  fi
  if printf '%s\n' "${TIMES[@]}" | grep -qx "$s"; then
    DISPLAY="$DISP" import -window root "$OUT-$s.png" 2>/dev/null && echo "captured $OUT-$s.png"
  fi
done
kill "$GAME" 2>/dev/null; sleep 1; kill -9 "$GAME" 2>/dev/null
kill "$XV" 2>/dev/null
wait 2>/dev/null
