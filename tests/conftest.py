import os
import sys

# The engine modules live at the repo root, not under tests/ or a package —
# add it to sys.path so `import memory_sync` etc. work the same way the
# `okfmem` dispatcher's runpy invocation resolves them.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
