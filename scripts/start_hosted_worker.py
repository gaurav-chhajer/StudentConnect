"""Background sign-in task: local fetching, offline similarity, and local SMTP."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from backend.hosting import load_hosted_environment

PROJECT_ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)


def main():
    """Load private configuration before importing modules that capture credentials."""
    handler = RotatingFileHandler(PROJECT_ROOT / "data" / "hosted_worker.log",
                                  maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    try:
        load_hosted_environment(PROJECT_ROOT / ".env")
        from scripts import local_worker

        logger.info("Sign-in worker starting: local database, five-minute fetching, offline similarity, SMTP; email threshold=60/100.")
        status = local_worker.run()
        logger.info("Sign-in worker stopped; result_code=%s", status)
        return status
    except Exception as error:
        # Final background-process boundary: never log private exception messages.
        logger.error("Sign-in worker stopped (%s); inspect configuration and hosted logs before restarting.",
                     type(error).__name__)
        return 1
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(previous_level)
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
