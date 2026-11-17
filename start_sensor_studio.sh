#!/bin/bash
set -euo pipefail

home_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
studio_args=("$@")

cd "$home_dir"
source myenv/bin/activate

# setup_env.sh ends with: exec "$@". Clear positional arguments while
# sourcing it so application arguments are not executed as a command.
set --
export PYTHONPATH="${PYTHONPATH:-}"
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
source openeb/build/utils/scripts/setup_env.sh

if ! python -m sensor_studio.build_native >/dev/null; then
    echo "WARNING: DVS native fast path was not built; Python fallback will be used" >&2
fi

exec python -m sensor_studio "${studio_args[@]}"
