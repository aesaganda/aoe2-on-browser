#!/usr/bin/env bash
# Next to each of the game's AVI cinematics (Indeo Video 5 + PCM), an MPEG-1 copy
# that the native host plays with pl_mpeg (src/linux/mciwnd.c, ADR-0004):
#
#   tools/transcode_videos.sh <install-root>
#
# ffmpeg runs here, at install time, on the owner's machine: the runtime carries no
# Indeo decoder. MPEG-1 has no 15 fps, so frames are doubled to 30. The copies stay
# in the install tree with the originals (ADR-0002: nothing derived goes in the repo).
set -euo pipefail
ROOT=${1:?usage: transcode_videos.sh <install-root>}
command -v ffmpeg >/dev/null || { echo "needs ffmpeg (apt install ffmpeg)" >&2; exit 69; }
n=0
while IFS= read -r -d '' avi; do
  mpg="${avi%.*}.mpg"
  [[ -s "$mpg" && "$mpg" -nt "$avi" ]] && continue
  echo "  $avi"
  ffmpeg -nostdin -loglevel error -y -i "$avi" \
    -c:v mpeg1video -q:v 2 -r 30 -c:a mp2 -b:a 192k -ar 44100 -ac 2 -f mpeg "$mpg.tmp"
  mv "$mpg.tmp" "$mpg"
  n=$((n + 1))
done < <(find "$ROOT" -iname '*.avi' -print0)
echo "videos: $n transcoded"
