"""Entry point for hosting (Streamlit Community Cloud): `streamlit run streamlit_app.py`.

Runs the app in src/courtvision/app whether or not the package is installed. With no
database built here, the app downloads the demo copy (see `courtvision demo`).
"""

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
runpy.run_path(str(ROOT / "src" / "courtvision" / "app" / "streamlit_app.py"), run_name="__main__")
