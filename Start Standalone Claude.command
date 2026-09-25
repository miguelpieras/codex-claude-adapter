#!/bin/sh
set -eu
adapter_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${CODEX_ADAPTER_PYTHON:-python3}" "$adapter_dir/claude_mode.py" launch
