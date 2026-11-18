#!/usr/bin/env bash
# Record a game with the *original* age2_x1.exe under Wine (prefix: wine_setup.sh),
# for the determinism check against the lifted build:
#
#   tools/oracle/wine_record.sh <install-root> <out.mgx> [seconds of play]
#   tools/oracle/replay_check.sh <install-root> <out.mgx>
#
# Everything runs inside this one invocation (WSL ends a command's processes with
# it, Wine's included): Xvfb, the game in an 800x600 Wine desktop (screen
# coordinates are the game's), the EULA, Single Player -> a player name ->
# Standard Game with Record Game ticked -> Start Game, some minutes of play by the
# two computer players and the idle villagers, then Quit -> Achievements -> Main
# Menu, which is when the game writes the recording's header (0x426F90), and exit.
# The player profile is the native build's too; it is moved aside for the run.
set -u
ROOT=$(realpath "${1:?usage: wine_record.sh <install-root> <out.mgx> [seconds]}")
OUT=${2:?out.mgx}
PLAY=${3:-240}
export WINEPREFIX=${WINEPREFIX:-$HOME/aoe2-recomp-work/wine} WINEDEBUG=-all
DISP=":${XVFB_DISPLAY:-82}"
LOG=/tmp/aoe2-wine-record
mkdir -p "$LOG"

X() { DISPLAY="$DISP" xdotool "$@"; }
click() { X mousemove "$1" "$2" sleep 0.3 click 1; }
shot() { DISPLAY="$DISP" import -window root "$LOG/$1.png"; }

wineserver -k 2>/dev/null
Xvfb "$DISP" -screen 0 1024x768x24 > "$LOG/xvfb.log" 2>&1 &
XVFB=$!
[[ -f "$ROOT/player.nfx" ]] && mv "$ROOT/player.nfx" "$ROOT/player.nfx.native"
before=$(ls "$ROOT/savegame"/*.mgx 2>/dev/null | wc -l)
cleanup() {
  wineserver -k 2>/dev/null; kill $XVFB 2>/dev/null
  rm -f "$ROOT/player.nfx"
  [[ -f "$ROOT/player.nfx.native" ]] && mv "$ROOT/player.nfx.native" "$ROOT/player.nfx"
}
trap cleanup EXIT
sleep 2

printf 'cd /d E:\\\r\nage2_x1\\age2_x1.exe NOSTARTUP\r\n' > "$WINEPREFIX/drive_c/aoe2.bat"
DISPLAY="$DISP" wine explorer /desktop=aoe2,800x600 cmd /c 'C:\aoe2.bat' > "$LOG/game.log" 2>&1 &
sleep 20; shot 1-start
click 244 482                     # the EULA's Accept, when it asks (harmless on a black screen)
sleep 25; shot 2-menu
click 370 25; sleep 4             # Single Player
X type --delay 80 Player; sleep 1; click 325 353; sleep 5      # New Player: name, OK
click 608 173; sleep 5            # Standard Game
click 751 442; sleep 1; shot 3-setup   # Record Game
click 119 569                     # Start Game
sleep 40; shot 4-game
sleep "$PLAY"; shot 5-played
X key F10; sleep 2; click 407 161; sleep 2; click 305 328; sleep 6; shot 6-achievements   # Quit, Yes
click 710 514; sleep 6; shot 7-menu                                                       # Main Menu

after=$(ls "$ROOT/savegame"/*.mgx 2>/dev/null | wc -l)
rec=$(ls -t "$ROOT/savegame"/*.mgx 2>/dev/null | head -1)
if (( after <= before )) || [[ -z "$rec" ]]; then echo "no recording was written (screens in $LOG)"; exit 1; fi
mv "$rec" "$OUT"
echo "recorded by the original under Wine: $OUT"
python3 "$(dirname "$0")/mgx.py" "$OUT" | head -3
