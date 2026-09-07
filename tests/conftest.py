"""Put the repo root on sys.path so `import app.…` works under plain `pytest`.

pytest's default (prepend) import mode inserts the *test file's* directory,
not the rootdir, so without this the `app` package is invisible to the suite.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
