#!/usr/bin/env bash
# A long-lived headless session for driving the native build step by step:
#
#   tools/native/session.sh start <install-root> [--hold]   # Xvfb + game in the background
#   tools/native/session.sh shot <file.png>         # capture the screen
#   tools/native/session.sh click <gx> <gy>         # click at *game* coordinates (800x600)
#   tools/native/session.sh type <text>             # type text, then
#   tools/native/session.sh key <keysym>            # e.g. Return, Escape
#   tools/native/session.sh log [n]                 # last n lines of the game's output
#   tools/native/session.sh stop
#
# Game coordinates are mapped to the screen through the picture's placement, read
# from the first screenshot's non-black bounding box (pcrecomp's screen.c scales the
# 800x600 picture into its window). The player's EULA decision comes from the
# environment (AOE2_ACCEPT_EULA), never from here.
set -u
DIR=${AOE2_SESSION_DIR:-/tmp/aoe2-session}
DISP=":${XVFB_DISPLAY:-78}"
mkdir -p "$DIR"
BIN="$(cd "$(dirname "$0")/../.." && pwd)/build/native/aoe2"

geom() {   # picture rectangle on screen: x y w h (cached)
  if [[ -s "$DIR/geom" ]]; then cat "$DIR/geom"; return; fi
  DISPLAY="$DISP" import -window root "$DIR/g.png" 2>/dev/null
  local g; g=$(convert "$DIR/g.png" -fuzz 3% -trim -format "%X %Y %w %h" info: 2>/dev/null | sed 's/+//g')
  # cache only a plausible picture (a black screen trims to 1x1)
  if [[ $(echo "$g" | awk '{print ($3 >= 400 && $4 >= 300)}') == 1 ]]; then echo "$g" > "$DIR/geom"; fi
  echo "$g"
}

case "${1:-}" in
  start)
    Xvfb "$DISP" -screen 0 1600x1200x24 >/dev/null 2>&1 & echo $! > "$DIR/xvfb.pid"
    sleep 1
    rm -f "$DIR/geom"
    ( cd / && DISPLAY="$DISP" SDL_AUDIODRIVER="${SDL_AUDIODRIVER:-dummy}" \
        "$BIN" "$2" > "$DIR/game.log" 2>&1 & echo $! > "$DIR/game.pid" )
    echo "started (display $DISP)"
    # WSL ends a session's processes with the command that started them: with --hold,
    # stay until the game exits (run this in the background, drive it from elsewhere).
    if [[ "${3:-}" == "--hold" ]]; then
      while kill -0 "$(cat "$DIR/game.pid")" 2>/dev/null; do sleep 2; done
      echo "game exited"; tail -5 "$DIR/game.log"
      kill "$(cat "$DIR/xvfb.pid")" 2>/dev/null
    fi;;
  shot)
    DISPLAY="$DISP" import -window root "$2" && echo "$2";;
  click)
    read -r x y w h < <(geom)
    sx=$(( x + $2 * w / 800 )); sy=$(( y + $3 * h / 600 ))
    DISPLAY="$DISP" xdotool mousemove "$sx" "$sy" sleep 0.3 click 1 && echo "click game ($2,$3) = screen ($sx,$sy)";;
  type)
    DISPLAY="$DISP" xdotool type --delay 80 "$2" && echo "typed";;
  key)
    DISPLAY="$DISP" xdotool key "$2" && echo "key $2";;
  log)
    tail -n "${2:-20}" "$DIR/game.log";;
  alive)
    kill -0 "$(cat "$DIR/game.pid")" 2>/dev/null && echo alive || echo dead;;
  stop)
    kill "$(cat "$DIR/game.pid" 2>/dev/null)" 2>/dev/null; sleep 1
    kill -9 "$(cat "$DIR/game.pid" 2>/dev/null)" 2>/dev/null
    kill "$(cat "$DIR/xvfb.pid" 2>/dev/null)" 2>/dev/null
    echo stopped;;
  *) sed -n 2,14p "$0"; exit 64;;
esac
