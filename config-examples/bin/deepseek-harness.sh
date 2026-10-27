#!/bin/bash
set -euo pipefail
: "${DEEPSEEK_API_KEY:*** export DEEPSEEK_API_KEY}"
export DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
DSH_BIN="${DSH_BIN:-/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh}"
exec "$DSH_BIN" --profile headless "$@"
