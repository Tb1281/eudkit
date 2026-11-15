import sys
from pathlib import Path

# Ensure repo root and py directory are on sys.path so tests can import
# both as `py` (when run without installation) and find sibling fixtures.
_py_dir = Path(__file__).resolve().parent.parent
_repo_root = _py_dir.parent

if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
if str(_py_dir) not in sys.path:
    sys.path.insert(0, str(_py_dir))
