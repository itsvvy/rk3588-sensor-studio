from __future__ import annotations

import os
import sys
from pathlib import Path


def _configure_openeb_environment() -> None:
    home = Path(__file__).resolve().parent.parent
    openeb = Path(
        os.environ.get("SENSOR_STUDIO_OPENEB_ROOT", home / "openeb")
    ).expanduser()
    build = openeb / "build"
    python_paths = [
        build / "py3",
        openeb / "sdk/modules/core_ml/python/pypkg",
        openeb / "sdk/modules/core/python/pypkg",
        openeb / "utils/python",
    ]
    existing = [item for item in os.environ.get("PYTHONPATH", "").split(os.pathsep) if item]
    for path in reversed(python_paths):
        value = str(path)
        if value not in existing:
            sys.path.insert(0, value)
            existing.insert(0, value)
    os.environ["PYTHONPATH"] = os.pathsep.join(existing)
    os.environ.setdefault(
        "MV_HAL_PLUGIN_PATH", str(build / "lib/metavision/hal/plugins")
    )
    os.environ.setdefault("HDF5_PLUGIN_PATH", str(build / "lib/hdf5/plugin"))
    library_path = str(build / "lib")
    current_library_path = os.environ.get("LD_LIBRARY_PATH", "")
    if library_path not in current_library_path.split(os.pathsep):
        os.environ["LD_LIBRARY_PATH"] = (
            f"{library_path}:{current_library_path}"
            if current_library_path
            else library_path
        )


_configure_openeb_environment()

from .app import main  # noqa: E402


raise SystemExit(main())
