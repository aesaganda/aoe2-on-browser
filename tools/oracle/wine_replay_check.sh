#!/usr/bin/env bash
# The determinism check the other way round: replay a recording (one the native build
# made) with the *original* age2_x1.exe under Wine, sync checking on (DONTIGNORESYNC).
#
#   tools/oracle/wine_replay_check.sh <install-root> <recording.mgx> [seconds]
#
# The same oracle as replay_check.sh, on the original's own x86 code: gdb attaches to
# the 32-bit game process and breaks at the out-of-sync stop (0x5FF53A) and at the
# replay update (0x5FF180) once its world time ([ecx+0x10]) passes the last checksum.
# The executable is not relocated, so those are its addresses. Everything runs inside
# this invocation (WSL ends a command's processes with it).
#
# Exit status: 0 in sync, 1 out of sync, 2 inconclusive.
set -u
ROOT=$(realpath "${1:?usage: wine_replay_check.sh <install-root> <recording.mgx> [seconds]}")
REC=${2:?recording}
HERE=$(cd "$(dirname "$0")" && pwd)
export WINEPREFIX=${WINEPREFIX:-$HOME/aoe2-recomp-work/wine} WINEDEBUG=-all
DISP=":${XVFB_DISPLAY:-82}"
LOG=/tmp/aoe2-wine-replay
mkdir -p "$LOG"
want=$(python3 "$HERE/mgx.py" --checksums "$REC")
last=$(python3 "$HERE/mgx.py" --last-checksum "$REC")
ms=$(python3 "$HERE/mgx.py" --duration "$REC")
SECS=${3:-$(( ms / 1000 + 60 ))}
echo "recording: $REC: $(( ms / 1000 )) s of game time, $want checksums; the original replays it under Wine"

X() { DISPLAY="$DISP" xdotool "$@"; }
click() { X mousemove "$1" "$2" sleep 0.3 click 1; }
shot() { DISPLAY="$DISP" import -window root "$LOG/$1.png"; }

wineserver -k 2>/dev/null
Xvfb "$DISP" -screen 0 1024x768x24 > "$LOG/xvfb.log" 2>&1 &
XVFB=$!
[[ -f "$ROOT/player.nfx" ]] && mv "$ROOT/player.nfx" "$ROOT/player.nfx.native"
cp "$REC" "$ROOT/savegame/0-oracle.mgx"
cleanup() {
  [[ -n "${GDB:-}" ]] && kill $GDB 2>/dev/null
  wineserver -k 2>/dev/null; kill $XVFB 2>/dev/null
  rm -f "$ROOT/savegame/0-oracle.mgx" "$ROOT/player.nfx"
  [[ -f "$ROOT/player.nfx.native" ]] && mv "$ROOT/player.nfx.native" "$ROOT/player.nfx"
}
trap cleanup EXIT
sleep 2

printf 'cd /d E:\\\r\nage2_x1\\age2_x1.exe NOSTARTUP DONTIGNORESYNC\r\n' > "$WINEPREFIX/drive_c/aoe2.bat"
DISPLAY="$DISP" wine explorer /desktop=aoe2,800x600 cmd /c 'C:\aoe2.bat' > "$LOG/game.log" 2>&1 &
sleep 20
click 244 482                     # the EULA's Accept, when it asks
sleep 25
click 370 25; sleep 4             # Single Player
X type --delay 80 Player; sleep 1; click 325 353; sleep 5      # New Player: name, OK
click 606 383; sleep 4            # Saved and Recorded Games
click 194 98; sleep 1; shot 1-list    # the first: 0-oracle

PID=$(pgrep -f 'age2_x1\.exe' | head -1)
[[ -z "$PID" ]] && { echo "the game is not running"; exit 2; }
cat > "$LOG/oracle.gdb" <<G
set pagination off
handle SIGSEGV SIGUSR1 SIGUSR2 SIGILL SIGBUS SIGALRM SIGPIPE SIGQUIT SIGINT nostop noprint pass
break *0x5FF180 if *(unsigned*)(\$ecx + 0x10) >= $((last + 2000))
commands
silent
printf "[oracle] reached world time %u\\n", *(unsigned*)(\$ecx + 0x10)
delete 1
continue
end
break *0x5FF53A
commands
silent
printf "[oracle] OUT OF SYNC\\n"
continue
end
continue
G
timeout "$SECS" gdb -q -batch -p "$PID" -x "$LOG/oracle.gdb" > "$LOG/oracle.log" 2>&1 &
GDB=$!
sleep 8
click 199 569                     # OK: load the replay
while kill -0 $GDB 2>/dev/null; do
  reached=$(grep -c 'reached world time' "$LOG/oracle.log"); oos=$(grep -c 'OUT OF SYNC' "$LOG/oracle.log")
  (( oos > 0 || reached > 0 )) && break
  sleep 2
done
shot 2-end
reached=$(grep -c 'reached world time' "$LOG/oracle.log"); oos=$(grep -c 'OUT OF SYNC' "$LOG/oracle.log")
echo "past the last checksum ($(( last / 1000 )) s): $( (( reached )) && echo yes || echo no), out-of-sync stops: $oos"
if (( oos > 0 )); then echo "OUT OF SYNC"; exit 1; fi
if (( ! reached )); then echo "INCONCLUSIVE: the replay did not get past the last checksum (see $LOG)"; exit 2; fi
echo "IN SYNC: $want of $want checksums matched"
