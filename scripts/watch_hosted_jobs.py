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


def run_cycle(provider="similarity", fetch_only=False):
    """Hosted storage is authoritative; private local embeddings stay in data/."""
    client = worker.HostedClient()
    try:
        status = worker.fetch_and_score(client, time.monotonic() + worker.MATCHING_SECONDS,
                                        fetch_only=fetch_only, provider=provider)
        if status:
            logger.error("Worker cycle incomplete; review logs before restarting.")
            return status
        if fetch_only:
            logger.info("Job fetch completed; no comparison or email delivery was performed.")
            return 0
        if provider == "similarity":
            logger.info("Local rankings saved. Similarity rankings do not trigger fit-score email alerts.")
            return 0
        return worker.send_emails(client, time.monotonic() + worker.EMAIL_SECONDS, immediate=True)
    finally:
        client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Enable live fetching with the selected mode")
    parser.add_argument("--provider", choices=("similarity", "groq"), default="similarity",
                        help="Local similarity by default; groq explicitly enables LLM scoring and email alerts")
    parser.add_argument("--fetch-only", action="store_true", help="Fetch jobs without comparison or email delivery")
    parser.add_argument("--once", action="store_true", help="Exit after one cycle")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if not args.run:
        logger.info("Preview only: provider=%s; no external calls made.", args.provider)
        return 0
    try:
        with exclusive_cycle(LOCK_PATH):
            while True:
                logger.info("Hosted job check started.")
                status = run_cycle(args.provider, fetch_only=args.fetch_only)
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
