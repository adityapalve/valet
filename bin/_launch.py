"""Run valet from this source checkout. No build or install needed: valet is stdlib-only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from valet.cli import main  # noqa: E402

sys.exit(main())
