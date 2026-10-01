import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Paths are fixed at import, so point them away from the real ones first.
_tmp = tempfile.mkdtemp(prefix="speakeasy-test-")
os.environ["XDG_RUNTIME_DIR"] = os.path.join(_tmp, "run")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_tmp, "config")
os.makedirs(os.environ["XDG_RUNTIME_DIR"])
