#!/bin/sh
# rr linux.kernel_patch_script — apply DE25 LCD kernel patches after clone.
# $1 = kernel source dir. Idempotent (skips already-applied patches).
set -e
KDIR="$1"
PDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$KDIR"
for p in "$PDIR"/*.patch; do
    [ -f "$p" ] || continue
    if git apply --reverse --check "$p" >/dev/null 2>&1; then
        echo "  fbtft patch already applied: $(basename "$p")"
        continue
    fi
    git apply "$p" && echo "  fbtft patch applied: $(basename "$p")"
done
