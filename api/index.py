import sys
from pathlib import Path

# Make the src package importable in Vercel's /var/task environment
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from arb_scanner.main import app  # noqa: F401 — Vercel discovers `app`
