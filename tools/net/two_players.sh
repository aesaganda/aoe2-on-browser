#!/usr/bin/env bash
# Two native games against the relay (tools/net/relay.py, already running), driven
# through the game's own multiplayer screens: A creates "Host's Game", B shows the
# games, joins it, both say ready, A starts. Screens go to /tmp/aoe2-{A,B}/*.png.
#
#   tools/net/two_players.sh <install-root> [seconds to watch the game] [gdb]
#
# Each game runs in tools/native/session.sh (its own Xvfb). With "gdb" both run
# under gdb, which prints a crash's backtrace into their game.log.
set -u
ROOT=${1:?usage: two_players.sh <install-root> [seconds] [gdb]}
WATCH=${2:-40}
HERE=$(cd "$(dirname "$0")" && pwd); S="$HERE/../native/session.sh"
export AOE2_ACCEPT_EULA=1 AOE2_TRACE_DPLAY=1
[[ "${3:-}" == gdb ]] && export AOE2_GDB=1
A() { AOE2_SESSION_DIR=/tmp/aoe2-A XVFB_DISPLAY=78 bash "$S" "$@" >/dev/null; }
B() { AOE2_SESSION_DIR=/tmp/aoe2-B XVFB_DISPLAY=77 bash "$S" "$@" >/dev/null; }
A stop; B stop
(AOE2_SESSION_DIR=/tmp/aoe2-A XVFB_DISPLAY=78 timeout 3600 bash "$S" start "$ROOT" --hold >/dev/null 2>&1 &)
(AOE2_SESSION_DIR=/tmp/aoe2-B XVFB_DISPLAY=77 timeout 3600 bash "$S" start "$ROOT" --hold >/dev/null 2>&1 &)
sleep 30
A click 309 228; B click 309 228; sleep 4                    # Multiplayer
A type Host; B type Guest; sleep 1; A click 325 353; B click 325 353; sleep 6   # names
A click 506 105; sleep 2; A click 600 456; sleep 6           # A: Online, Create
B click 506 105; sleep 2; B click 600 199; sleep 5           # B: Online, Show Games
B click 493 236; sleep 1; B click 600 399; sleep 8           # B: the game, Join
A click 360 456; sleep 1; A type "hello from host"; A key Return; sleep 2      # chat
B click 55 498; sleep 3; A click 55 498; sleep 3             # I'm Ready!
A shot /tmp/aoe2-A/lobby.png; B shot /tmp/aoe2-B/lobby.png
A click 119 569                                              # A: Start Game
sleep "$WATCH"
A shot /tmp/aoe2-A/game.png; B shot /tmp/aoe2-B/game.png
for s in A B; do printf '%s: ' $s; AOE2_SESSION_DIR=/tmp/aoe2-$s bash "$S" alive; done
