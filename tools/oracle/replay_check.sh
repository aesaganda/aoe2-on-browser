#!/usr/bin/env bash
# The determinism check: replay a recorded game (.mgx) in the native build with the
# game's own sync checking on, and report whether every checksum in it matched.
#
#   tools/oracle/replay_check.sh <install-root> <recording.mgx> [seconds]
#
# AoC writes a world checksum into its recordings about every 40 s of game time (a
# sync op with a 28-byte block, tools/oracle/mgx.py). With DONTIGNORESYNC on the
# command line a replay compares each one against its own world, and on a mismatch
# stops: "Out of sync replaying game. Replaying stopped." (0x5FF53A in 0x5FF180).
# This drives the menus to the replay, and gdb watches two places: the out-of-sync
# stop, and the replay's world time ([this+0x10] in 0x5FF180, each frame) passing the
# last checksum. Reaching that with no stop means every checksum matched. (The
# replay reads its file ahead of play, so reads of the blocks prove nothing.)
#
# Exit status: 0 in sync, 1 out of sync, 2 inconclusive (the last checksum not reached).
set -u
ROOT=${1:?usage: replay_check.sh <install-root> <recording.mgx> [seconds]}
REC=${2:?recording}
SECS=${3:-}
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$HERE/../.." && pwd)
S="$REPO/tools/native/session.sh"
PY=${PY:-python3}
cd "$REPO"

want=$($PY "$HERE/mgx.py" --checksums "$REC")
last=$($PY "$HERE/mgx.py" --last-checksum "$REC")
ms=$($PY "$HERE/mgx.py" --duration "$REC")
[[ -z "$SECS" ]] && SECS=$(( ms / 1000 + 60 ))    # the replay runs at about game speed (1.5x and up)
echo "recording: $REC: $(( ms / 1000 )) s of game time, $want checksums; watching for up to $SECS s"

# First in the Saved Games list, which is sorted by name.
cp "$REC" "$ROOT/savegame/0-oracle.mgx"
trap 'rm -f "$ROOT/savegame/0-oracle.mgx"; bash "$S" stop >/dev/null 2>&1' EXIT

oos_line=$(grep -n '^L_005FF53A:' build/lift/gen/recomp_*.c | head -1)
oos_file=$(basename "${oos_line%%:*}"); oos_no=$(echo "$oos_line" | cut -d: -f2)
cat > /tmp/aoe2-oracle.gdb <<G
set pagination off
handle SIGALRM SIGPIPE SIGUSR1 SIGUSR2 SIGSEGV nostop noprint pass
break sub_005FF180 if *(unsigned*)(g_mem_base + g_ecx + 0x10) >= $((last + 2000))
commands
silent
printf "[oracle] reached world time %u\n", *(unsigned*)(g_mem_base + g_ecx + 0x10)
delete 1
continue
end
break $oos_file:$((oos_no + 1))
commands
silent
printf "[oracle] OUT OF SYNC\n"
continue
end
continue
G

export AOE2_ACCEPT_EULA=1 AOE2_ARGS=DONTIGNORESYNC
bash "$S" stop >/dev/null 2>&1
timeout $((SECS + 200)) bash "$S" start "$ROOT" --hold >/dev/null 2>&1 &
sleep 25
# main menu -> Single Player (first run: a player name) -> Saved and Recorded Games -> first -> OK
bash "$S" click 370 25 >/dev/null; sleep 4
bash "$S" type Player >/dev/null; sleep 1; bash "$S" click 325 353 >/dev/null; sleep 5
bash "$S" click 606 383 >/dev/null; sleep 4
bash "$S" click 194 98 >/dev/null; sleep 1
PID=$(pgrep -f "/build/native/aoe2 " | head -1)
[[ -z "$PID" ]] && { echo "the game is not running"; exit 2; }
timeout "$SECS" gdb -q -batch -p "$PID" -x /tmp/aoe2-oracle.gdb > /tmp/aoe2-oracle.log 2>&1 &
GDB=$!
sleep 8
bash "$S" click 199 569 >/dev/null
while kill -0 $GDB 2>/dev/null; do
  reached=$(grep -c 'reached world time' /tmp/aoe2-oracle.log); oos=$(grep -c 'OUT OF SYNC' /tmp/aoe2-oracle.log)
  (( oos > 0 || reached > 0 )) && break
  sleep 2
done
kill $GDB 2>/dev/null; wait $GDB 2>/dev/null
reached=$(grep -c 'reached world time' /tmp/aoe2-oracle.log); oos=$(grep -c 'OUT OF SYNC' /tmp/aoe2-oracle.log)
bash "$S" shot /tmp/aoe2-oracle.png >/dev/null 2>&1
echo "past the last checksum ($(( last / 1000 )) s): $( (( reached )) && echo yes || echo no), out-of-sync stops: $oos"
if (( oos > 0 )); then echo "OUT OF SYNC"; exit 1; fi
if (( ! reached )); then echo "INCONCLUSIVE: the replay did not get past the last checksum"; exit 2; fi
echo "IN SYNC: $want of $want checksums matched"
