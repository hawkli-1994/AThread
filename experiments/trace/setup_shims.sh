#!/bin/bash
# Build the exec-trace shim and create the tool symlinks in trace/shim_bin/.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
gcc -O2 -o "$HERE/shim" "$HERE/shim.c"
mkdir -p "$HERE/shim_bin"
for t in python3 python python3.12 python3.13 python3.14 node nodejs git rg find bash sh \
         cat ls grep sed awk head tail wc sort uniq xargs stat file env which cut tr \
         dirname basename realpath readlink cp mv rm mkdir touch pwd ln chmod chown \
         date uname tar gzip du df ps kill echo printf true false \
         npm npx yarn pnpm pip pip3 uv cargo go rustc gcc g++ make cmake jq curl wget \
         tee comm diff patch test '['; do
    ln -sf "$HERE/shim" "$HERE/shim_bin/$t"
done
echo "shim built: $(ls "$HERE/shim_bin" | wc -l) tool shims -> $HERE/shim_bin"
