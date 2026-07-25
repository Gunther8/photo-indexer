import sys
import logging
from pathlib import Path

# Ensure the project root is on the path so sibling packages resolve correctly
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from utils.helpers import setup_logging
from ui.main_window import MainWindow

LOG_DIR = Path.home() / ".photo_indexer"


def main():
    setup_logging(LOG_DIR)
    logger = logging.getLogger(__name__)
    logger.info("Starting Photo Indexer")

    app = QApplication(sys.argv)
    app.setApplicationName("百度网盘照片索引器")


    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
