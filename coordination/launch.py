"""Launch with the base interpreter, avoiding Windows venv launcher stubs."""
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / ".coord-env" / "Lib" / "site-packages"))
runpy.run_path(str(root / "coordination" / "server.py"), run_name="__main__")
