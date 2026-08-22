"""M1.1 control panel: Flask dev server over an IngestManager.

All cameras begin stopped; the panel is what starts them. Camera edits
stay in-memory only — a restart of this process resets all state.

Usage:
    python scripts/run_control_panel.py [--host 127.0.0.1] [--port 5050] [--config PATH]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import load_config
from nvr.control.api import create_app
from nvr.ingest.manager import IngestManager


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--config", type=str, default="config/config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    cameras = {c.name: c for c in config.cameras}
    manager = IngestManager(config.cameras)  # cameras start stopped
    app = create_app(manager, cameras)
    print(f"control panel: http://{args.host}:{args.port}  ({len(cameras)} cameras)")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
