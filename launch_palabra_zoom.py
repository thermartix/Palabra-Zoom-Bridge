from __future__ import annotations

import importlib.util
import runpy
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
REQUIREMENTS_PATH = PROJECT_DIR / "requirements.txt"
BRIDGE_PATH = PROJECT_DIR / "palabra_zoom.py"

REQUIRED_IMPORTS = {
    "httpx": "httpx",
    "numpy": "numpy",
    "python-dotenv": "dotenv",
    "scipy": "scipy",
    "sounddevice": "sounddevice",
    "websockets": "websockets",
}


def missing_dependencies() -> list[str]:
    return [
        package
        for package, module_name in REQUIRED_IMPORTS.items()
        if importlib.util.find_spec(module_name) is None
    ]


def install_dependencies(missing: list[str]) -> None:
    print("Missing Python dependencies: " + ", ".join(missing))
    print("Installing Python dependencies from requirements.txt...")
    subprocess.check_call(
        [
            sys.executable,
            "-E",
            "-m",
            "pip",
            "install",
            "-r",
            str(REQUIREMENTS_PATH),
        ],
        cwd=PROJECT_DIR,
    )


def main() -> None:
    missing = missing_dependencies()
    if missing:
        install_dependencies(missing)

    sys.argv = [str(BRIDGE_PATH), *sys.argv[1:]]
    runpy.run_path(str(BRIDGE_PATH), run_name="__main__")


if __name__ == "__main__":
    main()
