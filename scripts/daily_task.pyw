"""Start ``courtvision daily`` with no console window, for the Windows scheduled task.

Run it with the base interpreter's ``pythonw.exe`` (the ``home`` in ``.venv/pyvenv.cfg``),
not ``.venv/Scripts/pythonw.exe``: uv's venv ``pythonw.exe`` is a console program, so it
opens a terminal window, and closing that window (or pressing Ctrl+C in it) stops the run.
Extra arguments go to ``courtvision daily`` (e.g. ``--dry-run``).
"""

import os
import runpy
import site
import sys
from pathlib import Path

venv = Path(__file__).resolve().parents[1] / ".venv"
site.addsitedir(str(venv / "Lib" / "site-packages"))  # the project and its dependencies
os.environ["COURTVISION_PYTHON"] = str(venv / "Scripts" / "python.exe")  # for its steps
sys.argv = ["courtvision", "daily", *sys.argv[1:]]
runpy.run_module("courtvision", run_name="__main__", alter_sys=True)
