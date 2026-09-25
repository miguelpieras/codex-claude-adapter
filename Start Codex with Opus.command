#!/bin/sh
set -eu
adapter_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$adapter_dir/codex-with-claude" --adapter-action launch
