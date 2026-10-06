"""Explicitly email the first owner invitation, linking an existing profile by email."""

import logging
import sqlite3
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path

from backend import accounts,pipeline_skeleton as pipeline


def main():
    logging.basicConfig(level=logging.INFO,format="%(levelname)s: %(message)s")
    try:
        email = input("Owner email (use the existing profile email to retain its data): ")
        confirmation = input("Send a real owner invitation email now? Type SEND: ")
        if confirmation != "SEND":
            logging.info("Owner invitation cancelled; no changes made")
            return 0
        with closing(sqlite3.connect(Path(pipeline.DB_PATH).as_uri()+"?mode=rw",uri=True,timeout=5)) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            accounts.issue_invitation(conn,email,datetime.now(timezone.utc),owner=True)
        logging.info("Owner invitation accepted by Gmail; enter its code on /register")
        return 0
    except (ValueError,RuntimeError,OSError,sqlite3.Error) as error:
        logging.error("Owner invitation failed (%s); check configuration, pending invitations and Gmail before retrying",type(error).__name__)
        return 1
    except (KeyboardInterrupt,EOFError):
        logging.warning("Owner setup interrupted; review pending invitations if sending had started")
        return 130


if __name__=="__main__":
    raise SystemExit(main())
