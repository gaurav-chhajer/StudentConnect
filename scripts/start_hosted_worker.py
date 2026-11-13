"""Background sign-in task: hosted database, Groq title filtering/scoring, hosted email."""

import logging
import os

from dotenv import dotenv_values
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
        # Explicitly configured provider key slots must replace stale inherited values.
        private_values=dotenv_values(PROJECT_ROOT / '.env', interpolate=False)
        for name in ('GROQ_API_KEY','GROQ_API_KEY_2','GROQ_API_KEY_3','GROQ_API_KEY_4','GROQ_API_KEY_5',
                     'GROQ_API_KEY_6','GROQ_API_KEY_7',
                     'TAVILY_API_KEY','TAVILY_API_KEY_2','TAVILY_API_KEY_3'):
            if name in private_values:
                os.environ[name]=private_values[name] or ''
        from scripts import watch_hosted_jobs

        logger.info("Sign-in worker starting: hosted database, five-minute fetching, local deterministic scoring and immediate match-only email.")
        status = watch_hosted_jobs.main(["--run", "--provider", "deterministic"])
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
