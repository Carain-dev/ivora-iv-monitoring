import os
import sys
import tempfile
from pathlib import Path

# Isolated SQLite DB and model folder for every test run; never touches real data.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite:///" + os.path.join(_tmp, "test.db")
os.environ["MODEL_DIR"] = os.path.join(_tmp, "models")
os.environ.pop("BLYNK_TOKEN", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
