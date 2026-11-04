"""PyInstaller entry point for the private WinUI/Python process protocol."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from upcam_app.worker_api import serve


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    if sys.argv[1:] == ["--smoke-test"]:
        from upcam_app.smoke_data import make_smoke_service
        with TemporaryDirectory(prefix="upcam-winui-synthetic-") as directory:
            serve(service=make_smoke_service(Path(directory)))
    elif sys.argv[1:]:
        sys.exit(2)
    else:
        serve()
