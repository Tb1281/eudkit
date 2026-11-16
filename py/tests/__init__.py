import sys
from pathlib import Path

# Ensure the repo root is on sys.path so tests can import `py` and
# `py.tests.*` without putting `py/` itself on sys.path (which would shadow
# stdlib modules such as `array`, `queue`, `random`, `string`, and `struct`).
_repo_root = Path(__file__).resolve().parent.parent.parent

if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
