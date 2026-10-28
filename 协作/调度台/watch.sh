#!/bin/bash
# Portable standard-library helper; paths follow this checkout or COLLAB_ROOT.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/state.py" watch "${1:-7200}"
