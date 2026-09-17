#!/usr/bin/env bash
# Build the vLLM env these scripts serve from. Idempotent.
#
# Separate from `gatemem` and `gatemem-auto` on purpose: vLLM pins torch
# aggressively, and letting it resolve against the benchmark's torch (or
# LangGraph's dependency tree) is the fight the Auto Research README warns
# about for langchain-core, one layer down.
set -euo pipefail
ENV_NAME="${VLLM_ENV_NAME:-gatemem-vllm}"
PREFIX="$HOME/miniconda3/envs/$ENV_NAME"

if [[ -x "$PREFIX/bin/vllm" ]]; then
    echo "vllm already installed at $PREFIX/bin/vllm ($("$PREFIX/bin/vllm" --version 2>/dev/null | head -1))"
    exit 0
fi
# conda-forge, not defaults: the default channels require accepting Anaconda's
# Terms of Service, which is a licensing decision and not one a setup script
# should make on anyone's behalf.
conda create -y -n "$ENV_NAME" -c conda-forge --override-channels python=3.11
"$PREFIX/bin/pip" install vllm
echo "installed: $("$PREFIX/bin/vllm" --version 2>/dev/null | head -1)"

# conda-forge's Python links against an ICU newer than this box's system
# libstdc++, which breaks `import sqlite3` inside the vllm entry point. The
# env needs its own libstdc++; common.sh then puts it first on LD_LIBRARY_PATH.
conda install -y -n "$ENV_NAME" -c conda-forge --override-channels libstdcxx-ng libgcc-ng
LD_LIBRARY_PATH="$PREFIX/lib" "$PREFIX/bin/vllm" --version
