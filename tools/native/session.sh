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
    # AOE2_GDB=1: under gdb, which on a fatal signal prints the host backtrace (the
    # lifted sub_XXXXXXXX frames), the guest registers and the guest function running.
    RUN=("$BIN" "$2")
    if [[ "${AOE2_GDB:-0}" == 1 ]]; then
      RUN=(gdb -q -batch -ex "handle SIGALRM SIGPIPE SIGUSR1 SIGUSR2 nostop noprint pass"
           -ex run -ex "bt 25"
           -ex 'printf "guest: cur_func %08x eax %08x ecx %08x edx %08x ebx %08x esp %08x ebp %08x esi %08x edi %08x\n", g_cur_func, g_eax, g_ecx, g_edx, g_ebx, g_esp, g_ebp, g_esi, g_edi'
           -ex "info registers rip" --args "${RUN[@]}")
    fi
    ( cd / && DISPLAY="$DISP" SDL_AUDIODRIVER="${SDL_AUDIODRIVER:-dummy}" \
        "${RUN[@]}" > "$DIR/game.log" 2>&1 & echo $! > "$DIR/game.pid" )
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
  to-game)
    # main menu -> Single Player -> (first run: New Player "Player") -> Standard Game -> Start Game
    S="$0"
    bash "$S" click 370 25; sleep 4
    bash "$S" click 423 298; bash "$S" type Player; sleep 1; bash "$S" click 325 353; sleep 5
    bash "$S" click 608 173; sleep 5
    bash "$S" click 119 569;;
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
