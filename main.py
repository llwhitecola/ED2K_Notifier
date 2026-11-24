"""GUI entry point for ED2K Notifier."""

import sys

from ed2k_notifier.config import DEFAULT_CONFIG_PATH
from ed2k_notifier.ed2k import HAS_FAST_MD4
from ed2k_notifier.gui import run_gui


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        import socks  # noqa: F401
        from PySide6.QtWidgets import QApplication
        from ed2k_notifier.gui import MainWindow

        app = QApplication.instance() or QApplication([])
        win = MainWindow()
        print(f"OK | fast_md4={HAS_FAST_MD4} | config={DEFAULT_CONFIG_PATH} | title={win.windowTitle()}")
        sys.exit(0)
    sys.exit(run_gui())
