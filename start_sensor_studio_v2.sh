#!/bin/bash
set -euo pipefail

home_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
studio_args=("$@")
dependency_root="${SENSOR_STUDIO_DEPENDENCY_ROOT:-$home_dir}"
if [[ ! -d "$dependency_root/myenv" && -d /home/astri/myenv ]]; then
    dependency_root=/home/astri
fi

cd "$home_dir"
source "$dependency_root/myenv/bin/activate"

# setup_env.sh ends with: exec "$@". Clear positional arguments while
# sourcing it so application arguments are not executed as a command.
set --
export PYTHONPATH="${PYTHONPATH:-}"
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
export SENSOR_STUDIO_OPENEB_ROOT="${SENSOR_STUDIO_OPENEB_ROOT:-$dependency_root/openeb}"
source "$SENSOR_STUDIO_OPENEB_ROOT/build/utils/scripts/setup_env.sh"

if ! python -m sensor_studio_v2.build_native >/dev/null; then
    echo "WARNING: DVS native fast path was not built; Python fallback will be used" >&2
fi

exec python -m sensor_studio_v2 "${studio_args[@]}"
