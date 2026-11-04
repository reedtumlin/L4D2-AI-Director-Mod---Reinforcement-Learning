#!/usr/bin/env bash
# Compile every plugin in sourcemod/scripting/ and install the .smx into the dedicated server.
# Usage: bash sourcemod/build.sh            (uses SM_DIR below)
#        SM_DIR=/c/other/sourcemod bash sourcemod/build.sh
set -euo pipefail

SM_DIR="${SM_DIR:-/c/l4d2_server/left4dead2/addons/sourcemod}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SPCOMP="$SM_DIR/scripting/spcomp.exe"

[ -x "$SPCOMP" ] || { echo "spcomp not found at $SPCOMP" >&2; exit 1; }

for src in "$HERE"/scripting/*.sp; do
    name="$(basename "$src" .sp)"
    echo "== $name"
    "$SPCOMP" "$src" -i"$SM_DIR/scripting/include" -i"$HERE/scripting/include" \
        -o"$SM_DIR/plugins/$name.smx" | grep -vE "^(SourcePawn Compiler|Copyright)" || true
    [ -f "$SM_DIR/plugins/$name.smx" ] || { echo "FAILED: $name" >&2; exit 1; }
done
echo "Installed to $SM_DIR/plugins (restart server or 'sm plugins reload <name>')."
