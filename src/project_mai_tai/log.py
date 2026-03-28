from __future__ import annotations

import logging
import sys


def configure_logging(service_name: str, level: str = "INFO") -> logging.Logger:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    logger = logging.getLogger(service_name)
    logger.debug("logging configured")
    return logger
