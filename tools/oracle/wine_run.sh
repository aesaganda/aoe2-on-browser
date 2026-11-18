#!/usr/bin/env bash
# Run the original age2_x1.exe under Wine (prefix from wine_setup.sh) on its own Xvfb,
# in an 800x600 virtual desktop, so tools/native/session.sh can drive it like the
# native build:
#
#   tools/oracle/wine_run.sh start [args...]   # e.g. DONTIGNORESYNC (WSL: run it in the
#                                              # background; it holds until Wine exits)
#   tools/oracle/wine_run.sh stop
#   XVFB_DISPLAY=79 AOE2_SESSION_DIR=/tmp/aoe2-wine tools/native/session.sh click|type|key|shot ...
#
# The game starts in E:\ (the install root), as the shortcut does.
set -u
export WINEPREFIX=${WINEPREFIX:-$HOME/aoe2-recomp-work/wine} WINEDEBUG=${WINEDEBUG:--all}
DISP=":${XVFB_DISPLAY:-79}"
DIR=${AOE2_SESSION_DIR:-/tmp/aoe2-wine}
mkdir -p "$DIR"
case "${1:-}" in
  start)
    shift
    Xvfb "$DISP" -screen 0 1024x768x24 > "$DIR/xvfb.log" 2>&1 & echo $! > "$DIR/xvfb.pid"
    sleep 1
    rm -f "$DIR/geom"
    printf 'cd /d E:\\\r\nage2_x1\\age2_x1.exe %s\r\n' "$*" > "$WINEPREFIX/drive_c/aoe2.bat"
    ( DISPLAY="$DISP" wine explorer /desktop=aoe2,800x600 cmd /c 'C:\aoe2.bat' > "$DIR/game.log" 2>&1 & echo $! > "$DIR/game.pid" )
    echo "started under wine (display $DISP)"
    # WSL ends a command's processes with it: stay while Wine runs
    sleep 5
    while pgrep -x wineserver >/dev/null; do sleep 2; done
    echo "wine exited"; kill "$(cat "$DIR/xvfb.pid")" 2>/dev/null;;
  stop)
    wineserver -k 2>/dev/null; sleep 1
    kill "$(cat "$DIR/xvfb.pid" 2>/dev/null)" 2>/dev/null; pkill -f "Xvfb $DISP" 2>/dev/null
    echo stopped;;
  *) sed -n 2,11p "$0"; exit 64;;
esac
