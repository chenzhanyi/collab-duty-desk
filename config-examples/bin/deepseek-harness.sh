#!/bin/bash
# DeepSeek Harness · headless 适配器（公开示例版，不读取任何本地私有配置）
set -euo pipefail
: "${DEEPSEEK_API_KEY:?请先 export DEEPSEEK_API_KEY}"
export DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
DSH_BIN="${DSH_BIN:-/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh}"
exec "$DSH_BIN" --profile headless "$@"
