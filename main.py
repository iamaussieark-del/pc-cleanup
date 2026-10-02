"""Launch PC Cleanup."""

from __future__ import annotations

import ctypes
import os
import sys
import traceback


def main() -> None:
    if os.name != "nt":
        raise SystemExit("PC Cleanup runs on Windows.")
    if sys.version_info < (3, 10):
        # pythonw has no console, so say it in a window.
        message = f"PC Cleanup needs Python 3.10 or newer. This is Python {sys.version.split()[0]}.\n\nInstall a newer one from python.org."
        ctypes.windll.user32.MessageBoxW(None, message, "PC Cleanup", 0x10)
        raise SystemExit(message)
    if os.environ.get("PROCESSOR_ARCHITEW6432"):
        # 32-bit Python on 64-bit Windows is sent to SysWOW64 instead of System32,
        # where the driver tools and the driver store live.
        message = "PC Cleanup needs the 64-bit Python on 64-bit Windows.\n\nInstall the 64-bit Python from python.org (the default download)."
        ctypes.windll.user32.MessageBoxW(None, message, "PC Cleanup", 0x10)
        raise SystemExit(message)
    try:
        # Its own taskbar identity, so the window icon shows instead of Python's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PcCleanup.App")
    except (AttributeError, OSError):
        pass
    if "--self-test" in sys.argv:
        from cleanup_app.selftest import self_test

        if sys.stdout is None:
            # The exe has no console, so the result goes to a file in the app's folder.
            from cleanup_app.util import app_dir

            sys.stdout = sys.stderr = open(os.path.join(app_dir(), "selftest.log"), "w", encoding="utf-8")
        raise SystemExit(self_test())
    if "--no-admin" not in sys.argv:
        from cleanup_app.util import relaunch_as_admin

        if relaunch_as_admin():
            return
    try:
        from cleanup_app.ui import CleanupApp

        app = CleanupApp()
        app.mainloop()
    except Exception as exc:
        try:
            from cleanup_app.util import log

            log(traceback.format_exc())
        except Exception:
            pass
        ctypes.windll.user32.MessageBoxW(None, f"PC Cleanup could not start.\n\n{exc}", "PC Cleanup", 0x10)
        raise


if __name__ == "__main__":
    main()
