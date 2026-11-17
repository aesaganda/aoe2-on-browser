#!/usr/bin/env bash
# Lay out an Age of Empires II + The Conquerors 1.0 install from the owner's two
# disc images, the way the installers would, without running anything from the
# discs: ISO and CAB archives are only unpacked (7z, cabextract).
#
#   tools/install_from_iso.sh <AoE2.iso> <AoE2_Conquerors.iso> <age2_x1.exe> <out-root>
#
# <age2_x1.exe> is the owner's unprotected executable (orig/, ADR-0002). The retail
# disc ships it SafeDisc-wrapped (AGE2_X1.EXE loader + AGE2_X1.ICD), which the port
# cannot run. Result: <out-root>/age2_x1/age2_x1.exe, data/, sound/, ... (README).
set -euo pipefail
AOK_ISO="$1"; AOC_ISO="$2"; EXE="$3"; OUT="$4"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "unpacking disc images..."
7z x -y -o"$WORK/aok" "$AOK_ISO" >/dev/null || true     # 7z flags a harmless "Open Error" on these images
7z x -y -o"$WORK/aoc" "$AOC_ISO" >/dev/null || true
[[ -f "$WORK/aok/GAME/EMPIRES2.CAB" && -f "$WORK/aoc/MSGAME.CAB" ]] || { echo "not the expected discs"; exit 1; }

mkdir -p "$OUT"
echo "base game (EMPIRES2.CAB)..."
cabextract -q -d "$OUT" "$WORK/aok/GAME/EMPIRES2.CAB"
echo "expansion (MSGAME.CAB), over the base where both have a file..."
cabextract -q -d "$OUT" "$WORK/aoc/MSGAME.CAB"

# Lower-case a tree, merging directories that differ only in case (Data/ and DATA/).
# The game names files without regard to case, as Windows does; win32hle matches without
# case too, but one spelling per name keeps host tools and later copies consistent.
lower_tree() {
  find "$1" -mindepth 1 -depth -name '*[A-Z]*' -print0 | while IFS= read -r -d '' p; do
    d=$(dirname "$p"); l=$(basename "$p" | tr 'A-Z' 'a-z')
    if [[ -d "$p" && -d "$d/$l" ]]; then cp -rf "$p/." "$d/$l/"; rm -rf "$p"; else mv -f "$p" "$d/$l"; fi
  done
}
lower_tree "$OUT"
lower_tree "$WORK/aok/GAME"
lower_tree "$WORK/aoc/GAME"

# Files the setup programs copy from the disc as they are (some under 8.3 names);
# the expansion's copy wins where both discs have one.
mkdir -p "$OUT/data" "$OUT/age2_x1" "$OUT/sound" "$OUT/campaign" "$OUT/avi" "$OUT/savegame" "$OUT/scenario"
cp -f "$WORK/aok/GAME/data/empires2.dat"  "$OUT/data/empires2.dat"
cp -f "$WORK/aoc/GAME/data/empire_1.dat"  "$OUT/data/empires2_x1.dat"
cp -f "$WORK/aok/GAME/language.dll"       "$OUT/language.dll"
cp -f "$WORK/aoc/GAME/langua_1.dll"       "$OUT/language_x1.dll"
for d in sound campaign avi; do
  cp -rf "$WORK/aok/GAME/$d/." "$OUT/$d/"
  cp -rf "$WORK/aoc/GAME/$d/." "$OUT/$d/"
done
cp -f "$EXE" "$OUT/age2_x1/age2_x1.exe"
# AoC detects the base game by FindFirstFile("empires2.exe") in the install root; it
# never runs it. The base installer puts the disc's EMPIRES2.EXE there, and so do we.
cp -f "$WORK/aok/GAME/empires2.exe" "$OUT/empires2.exe"
# The Conquerors disc itself, for the host to present as a CD-ROM drive: the game
# checks for it before single player (registry CDPath -> GetDriveType == CDROM ->
# GetVolumeInformation label), as Windows would see the mounted image. Its contents
# and its ISO 9660 volume label (primary volume descriptor, offset 0x8028, 32 bytes).
rm -rf "$OUT/cdrom" && mkdir -p "$OUT/cdrom"
cp -r "$WORK/aoc/." "$OUT/cdrom/"
dd if="$AOC_ISO" bs=1 skip=$((0x8028)) count=32 2>/dev/null | tr -d ' \000' > "$OUT/cdrom.label"   # padded with spaces; d-characters have none

# The licence text the game's EULA step refers to, and the readme
for f in EULAX.RTF READMEX.RTF; do
  [[ -f "$WORK/aoc/$f" ]] && cp -f "$WORK/aoc/$f" "$OUT/$(echo "$f" | tr 'A-Z' 'a-z')"
done
echo "installed into $OUT ($(du -sh "$OUT" | cut -f1))"
