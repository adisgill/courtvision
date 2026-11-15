"""Entry point for hosting (Streamlit Community Cloud): `streamlit run streamlit_app.py`.

Runs the app in src/courtvision/app whether or not the package is installed. With no
database built here, the app downloads the demo copy (see `courtvision demo`).
"""

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _drop_changed_modules() -> None:
    """A redeploy updates the files, but the server keeps the modules it has imported, and
    Streamlit notices changed files only while someone has the app open (a push at night
    left the old pages running). This script is read afresh on every run: when any file of
    the package changed since the last run, drop the package so this run imports the new
    code."""
    files = {str(p): p.stat().st_mtime for p in (SRC / "courtvision").rglob("*.py")}
    if getattr(sys, "_courtvision_files", None) != files:
        for name in [n for n in sys.modules if n == "courtvision" or n.startswith("courtvision.")]:
            sys.modules.pop(name, None)
        sys._courtvision_files = files


_drop_changed_modules()
runpy.run_path(str(SRC / "courtvision" / "app" / "streamlit_app.py"), run_name="__main__")
