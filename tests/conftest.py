import os
import sys
import tempfile
from pathlib import Path

# Tests never write into the desk's real logs/.
os.environ.setdefault("DESK_LOG_DIR", tempfile.mkdtemp(prefix="desk-test-logs-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
