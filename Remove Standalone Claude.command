#!/bin/sh
set -eu
adapter_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$adapter_dir/claude_mode.py" remove
