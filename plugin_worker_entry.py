"""Private Workbench backend entry; standalone worker_entry.py stays separate."""

from upcam_app.plugin_worker import main


if __name__ == "__main__":
    raise SystemExit(main())
