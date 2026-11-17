from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE = PACKAGE_DIR / "native" / "dvs_fastpath.cpp"
OPENEB = ROOT / "openeb"
OUTPUT = PACKAGE_DIR / (
    f"_dvs_fastpath{sysconfig.get_config_var('EXT_SUFFIX')}"
)


def extension_is_current() -> bool:
    return OUTPUT.is_file() and OUTPUT.stat().st_mtime >= SOURCE.stat().st_mtime


def build(force: bool = False) -> Path:
    if not force and extension_is_current():
        return OUTPUT

    pybind_include = OPENEB / "pybind11-2.11.0" / "include"
    openeb_lib = OPENEB / "build" / "lib"
    include_paths = [
        Path(sysconfig.get_paths()["include"]),
        pybind_include,
        OPENEB / "hal" / "cpp" / "include",
        OPENEB / "sdk" / "modules" / "base" / "cpp" / "include",
    ]
    missing = [path for path in include_paths if not path.exists()]
    if missing:
        raise RuntimeError(
            "missing native build dependency: "
            + ", ".join(str(path) for path in missing)
        )

    command = [
        os.environ.get("CXX", "c++"),
        "-O3",
        "-DNDEBUG",
        "-fPIC",
        "-shared",
        "-std=c++17",
        "-Wall",
        "-Wextra",
    ]
    command.extend(f"-I{path}" for path in include_paths)
    command.extend(
        [
            str(SOURCE),
            f"-L{openeb_lib}",
            "-Wl,-rpath," + str(openeb_lib),
            "-lmetavision_hal",
            "-o",
            str(OUTPUT),
        ]
    )
    subprocess.run(command, check=True)
    return OUTPUT


if __name__ == "__main__":
    path = build(force="--force" in sys.argv[1:])
    print(path)
