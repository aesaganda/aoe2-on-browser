#!/usr/bin/env bash
# Headless Ghidra auto-analysis + JSON export.
#   tools/ghidra/run.sh <exe> <out.json>
# The Ghidra project lives outside the repo (Linux FS is much faster than drvfs).
set -euo pipefail
EXE="$1"; OUT="$2"
TOOLS_HOME="${AOE2R_TOOLS:-$HOME/.local/share/aoe2-recomp}"
GHIDRA_DIR="${GHIDRA_DIR:-$(ls -d "$TOOLS_HOME"/ghidra_*_PUBLIC | tail -1)}"
# Ghidra rejects project paths with a component starting with '.', so no ~/.cache here.
PROJ_DIR="${GHIDRA_PROJ:-$HOME/aoe2-recomp-work/ghidra}"
NAME=$(basename "$EXE" .exe)
mkdir -p "$PROJ_DIR" "$(dirname "$OUT")"
OUT_ABS="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="${OUT%.json}.log"
# analyzeHeadless prompts for a JDK when it can't find one; give it JAVA_HOME instead.
# Ghidra 12.x needs JDK 25 (application.java.min in Ghidra/application.properties).
if [[ -z "${JAVA_HOME:-}" ]]; then
  for j in /usr/lib/jvm/java-25-openjdk-* /usr/lib/jvm/java-2[5-9]-*; do
    [[ -x "$j/bin/java" ]] && { export JAVA_HOME="$j"; break; }
  done
fi

start=$(date +%s)
"$GHIDRA_DIR/support/analyzeHeadless" "$PROJ_DIR" "$NAME" \
  -import "$EXE" -overwrite \
  -max-cpu "${GHIDRA_CPUS:-8}" \
  -scriptPath "$SCRIPT_DIR" \
  -postScript ExportAnalysis.java "$OUT_ABS" \
  > "$LOG" 2>&1 || { tail -40 "$LOG"; exit 1; }
end=$(date +%s)
echo "ghidra: analysed $EXE in $((end-start))s -> $OUT"
grep -E "ERROR|Exception" "$LOG" | head -5 || true
