"""Run hosted matching and immediate alerts from this computer; Ctrl+C stops it."""

import argparse
import logging
import time
from pathlib import Path

import requests

from scripts import run_cloud_worker as worker
from scripts.run_pipeline import exclusive_cycle

logger = logging.getLogger(__name__)
CHECK_INTERVAL_SECONDS = 5 * 60
LOCK_PATH = Path(__file__).resolve().parents[1] / "data" / "hosted_worker.lock"


def run_cycle():
    """Keep credentials/resumes in memory; hosted storage remains authoritative."""
    client = worker.HostedClient()
    try:
        status = worker.fetch_and_score(client, time.monotonic() + worker.MATCHING_SECONDS)
        if status:
            logger.error("Matching incomplete; worker stopped before email. Review logs before restarting.")
            return status
        return worker.send_emails(client, time.monotonic() + worker.EMAIL_SECONDS, immediate=True)
    finally:
        client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Enable real ATS/Groq requests and hosted Gmail delivery")
    parser.add_argument("--once", action="store_true", help="Exit after one cycle")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if not args.run:
        logger.info("Preview only: --run enables hosted fetching, scoring and immediate emails; no external calls made.")
        return 0
    try:
        with exclusive_cycle(LOCK_PATH):
            while True:
                logger.info("Hosted job check started.")
                status = run_cycle()
                if status or args.once:
                    return status
                logger.info("Cycle complete; checking again in five minutes. Ctrl+C stops the worker.")
                for _ in range(CHECK_INTERVAL_SECONDS // 30):
                    time.sleep(30)
    except KeyboardInterrupt:
        logger.warning("Worker stopped; review any hosted email pause marker if delivery was interrupted.")
        return 130
    except (requests.RequestException, OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        logger.error("Local worker stopped (%s); inspect configuration and hosted logs before restarting.", type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
