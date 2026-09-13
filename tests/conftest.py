import os
import sys

# Make `import config` and `import cardealfindr` work when pytest runs from anywhere.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
