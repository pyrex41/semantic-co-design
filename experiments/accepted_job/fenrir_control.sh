#!/bin/sh
# Usage: sh experiments/accepted_job/fenrir_control.sh ../fenrir
set -eu
repo=${1:?supply Fenrir checkout}
adapter=$(cd "$(dirname "$0")" && pwd)/fenrir_observations.py
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
git -C "$repo" archive -o "$scratch/source.tar" da55d436f40bc15aedd16eed2305f4694d2bc6e7
tar -xf "$scratch/source.tar" -C "$scratch"
cd "$scratch"
pinned() {
  nix --extra-experimental-features 'nix-command flakes' build --no-link --print-out-paths ".#$1"
}
FENRIR_SHEN_JIT=$(pinned shen-jit)
maude_root=$(pinned maude)
MAUDE="$maude_root/bin/maude"
export FENRIR_SHEN_JIT MAUDE
echo "Shen/JIT $FENRIR_SHEN_JIT"
echo "Maude $MAUDE"
sh verification/maude/run-accepted-job-retry.sh "$scratch/control"
python3 "$adapter" "$scratch/control"
