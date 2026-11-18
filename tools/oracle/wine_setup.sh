#!/usr/bin/env bash
# A Wine prefix that runs the *original* age2_x1.exe on the same install tree, as the
# reference for the determinism check (tools/oracle/replay_check.sh, docs/phase1-bringup.md):
#
#   tools/oracle/wine_setup.sh <install-root> [prefix]      (default ~/aoe2-recomp-work/wine)
#
#   E:  the install root (age2_x1\age2_x1.exe, data\, savegame\, ...)
#   D:  the Conquerors disc tree (<install>/cdrom), a CD-ROM drive labelled as the disc
#       (cdrom.label; Wine reads a drive's label from .windows-label)
#   the registry keys the installer writes (the same as src/linux/main.c seeds)
#
# Wine runs the game's x86 code itself, so a game it records is the original's.
set -euo pipefail
ROOT=$(realpath "${1:?usage: wine_setup.sh <install-root> [prefix]}")
export WINEPREFIX=${2:-$HOME/aoe2-recomp-work/wine} WINEARCH=win32 WINEDEBUG=-all
export WINEDLLOVERRIDES="mscoree,mshtml="          # no Mono / Gecko download prompts
command -v wine >/dev/null || { echo "needs wine with 32-bit support (wine32:i386)" >&2; exit 69; }

[[ -d "$WINEPREFIX/drive_c" ]] || wineboot -i
wineserver -w

ln -sfn "$ROOT" "$WINEPREFIX/dosdevices/e:"
ln -sfn "$ROOT/cdrom" "$WINEPREFIX/dosdevices/d:"
cp -f "$ROOT/cdrom.label" "$ROOT/cdrom/.windows-label"
# The Conquerors' EULA DLL sits next to the game on a real install
[[ -f "$ROOT/ebueulax.dll" ]] || cp -f "$ROOT/cdrom/GAME/ebueulax.dll" "$ROOT/ebueulax.dll"

reg=$(mktemp --suffix=.reg)
cat > "$reg" <<'REG'
REGEDIT4

[HKEY_LOCAL_MACHINE\Software\Wine\Drives]
"d:"="cdrom"
"e:"="hd"

[HKEY_LOCAL_MACHINE\Software\Microsoft\Microsoft Games\Age of Empires II: The Conquerors Expansion\1.0]
"EXE Path"="E:\\age2_x1"
"CDPath"="D:\\"

[HKEY_LOCAL_MACHINE\Software\Microsoft\Microsoft Games\Age of Empires\2.0]
"EXE Path"="E:\\"
"CDPath"="D:\\"

[HKEY_CURRENT_USER\Software\Wine\Drivers]
"Audio"=""

; Xvfb dies when Wine changes its mode for the game's 800x600x8 (the game runs in a
; virtual desktop anyway)
[HKEY_CURRENT_USER\Software\Wine\X11 Driver]
"UseXRandR"="N"
"UseXVidMode"="N"

; DirectDraw in software: wined3d's OpenGL path loses its X connection under Xvfb
[HKEY_CURRENT_USER\Software\Wine\Direct3D]
"renderer"="gdi"
REG
wine regedit /S "$(winepath -w "$reg")"
wineserver -w
rm -f "$reg"
echo "wine prefix ready: $WINEPREFIX (E: = $ROOT, D: = cdrom \"$(cat "$ROOT/cdrom.label")\")"
