"""Run hosted matching and immediate alerts from this computer; Ctrl+C stops it."""

import argparse
import logging
import threading
import time
from pathlib import Path

import requests

from scripts import run_cloud_worker as worker
from scripts.run_pipeline import exclusive_cycle

logger = logging.getLogger(__name__)
CHECK_INTERVAL_SECONDS = 5 * 60
LOCK_PATH = Path(__file__).resolve().parents[1] / "data" / "hosted_worker.lock"
QUEUE_POLL_SECONDS = 30
WRITE_LOCK_SECONDS = 120


class _QueuedClient:
    """Each loop owns a session; only hosted writes share the short critical section."""

    def __init__(self, writes, stop, ready):
        self.client = worker.HostedClient()
        self.writes, self.stop, self.ready = writes, stop, ready

    def call(self, path, data=None):
        if self.stop.is_set():
            raise RuntimeError("Queue worker is stopping")
        if data is None:
            return self.client.call(path)
        if not self.writes.acquire(timeout=WRITE_LOCK_SECONDS):
            raise RuntimeError("Hosted write lock timed out")
        try:
            if self.stop.is_set():
                raise RuntimeError("Queue worker is stopping")
            result = self.client.call(path, data)
            if path == "snapshot":
                self.ready.set()
            return result
        finally:
            self.writes.release()

    def close(self):
        self.client.close()


def _fetch_loop(client, stop):
    while not stop.is_set():
        started = time.monotonic()
        logger.info("Independent fetch started; successful snapshots enter the durable queue.")
        status = worker._fetch_and_score(client, started + worker.MATCHING_SECONDS,
                                        fetch_only=True, pool=None, stop=stop)
        if status:
            logger.error("Fetch loop stopped; successful snapshots remain queued. Review logs before restarting.")
            return status
        delay = max(0, started + CHECK_INTERVAL_SECONDS - time.monotonic())
        logger.info("Fetch completed; next fetch in %.1f seconds, independent of scoring/email.", delay)
        if stop.wait(delay):
            break
    return 0


def _score_loop(client, stop, ready):
    while not stop.is_set():
        ready.clear()
        logger.info("Queue scoring batch started; fetching runs independently.")
        status = worker.score_pending(client, time.monotonic() + worker.MATCHING_SECONDS, stop)
        if status or stop.is_set():
            if status and not stop.is_set():
                logger.error("Scoring loop stopped; pending jobs remain queued. Review logs before restarting.")
            return status
        status = worker.send_emails(client, time.monotonic() + worker.EMAIL_SECONDS, immediate=True)
        if status:
            logger.error("Email loop stopped; inspect hosted email state before restarting.")
            return status
        ready.wait(QUEUE_POLL_SECONDS)
    return 0


def run_queued():
    """One fetcher and one scoring/email consumer share the existing persisted queue."""
    stop, ready = threading.Event(), threading.Event()
    writes = threading.Lock()
    statuses = [0, 0]  # Each slot has one writer; the owner reads only after joining.

    def run(index):
        client = None
        try:
            client = _QueuedClient(writes, stop, ready)
            statuses[index] = (_fetch_loop(client, stop) if index == 0 else
                               _score_loop(client, stop, ready))
        except Exception as error:
            statuses[index] = 1
            logger.error("%s loop stopped (%s); inspect logs before restarting. The other loop remains independent.",
                         "Fetch" if index == 0 else "Scoring/email", type(error).__name__)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as error:
                    statuses[index] = 1
                    logger.error("Queue client cleanup failed (%s); inspect logs before restarting.", type(error).__name__)

    with exclusive_cycle(LOCK_PATH):
        threads = [threading.Thread(target=run, args=(index,), name=name, daemon=True)
                   for index, name in enumerate(("hosted-fetch", "hosted-score-email"))]
        started = []
        try:
            for thread in threads:
                thread.start()
                started.append(thread)
            while any(thread.is_alive() for thread in started):
                for thread in started:
                    thread.join(timeout=0.5)
        except KeyboardInterrupt:
            logger.warning("Queue worker stopping; in-flight requests must finish before shutdown.")
            return 130
        finally:
            stop.set()
            ready.set()
            # Let bounded hosted requests finish. Longer read-only ATS downloads
            # cannot write after stop is set; daemon threads bound process exit.
            deadline = time.monotonic() + WRITE_LOCK_SECONDS
            for thread in started:
                thread.join(timeout=max(0, deadline - time.monotonic()))
                if thread.is_alive():
                    statuses[0] = 1
                    logger.error("Queue shutdown timed out; inspect hosted write/email state before restarting.")
        return int(any(statuses))


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
