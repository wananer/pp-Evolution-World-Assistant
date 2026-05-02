import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST_SHIMS = Path(__file__).resolve().parent / "host_shims"
if str(HOST_SHIMS) not in sys.path:
    sys.path.insert(0, str(HOST_SHIMS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
