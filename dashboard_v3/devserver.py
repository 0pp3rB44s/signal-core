"""Local development launcher — 127.0.0.1 only, never a public listener.

Runs on DASHBOARD_DEV_PORT (default 8531) so it can never collide with the
production dashboard port. It reads the same state the engine writes, but only
ever reads.
"""
from __future__ import annotations

import os

# Bind loopback before the app module is imported so it cannot inherit a
# publicly-routable host from the environment.
os.environ.setdefault("DASHBOARD_HOST", "127.0.0.1")

from dashboard_v3.app import app  # noqa: E402

if __name__ == "__main__":
    host = "127.0.0.1"
    port = int(os.environ.get("DASHBOARD_DEV_PORT", "8531"))
    print(f"dashboard_v3 dev server on http://{host}:{port} (loopback only)")
    app.run(host=host, port=port, debug=False, threaded=True)
