"""``python -m courtvision.tracking ...``: the same commands as ``courtvision tracking``."""

import logging
import sys

from courtvision.tracking.cli import app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    app()
