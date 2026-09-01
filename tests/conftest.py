"""Make the plugin package importable when tests run from the AstrBot root.

Usage (from the AstrBot repository root):

    uv run pytest data/plugins/astrbot_plugin_memoir/tests -v
"""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
