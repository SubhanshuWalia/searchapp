import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOG_DIR / "search_app.log"


def setup_logging():
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )

    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()

    if not root_logger.handlers:
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)