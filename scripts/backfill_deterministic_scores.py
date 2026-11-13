"""Preview or safely replace stale local fit scores with deterministic results."""

import argparse
import json
import logging
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from backend import pipeline_skeleton as pipeline, store

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


def _backup(conn, directory):
    directory.mkdir(parents=True, exist_ok=True)
    name = "jobs-before-deterministic-rescore-{}-{}.db".format(
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), uuid.uuid4().hex[:8])
    destination = directory / name
    with destination.open("xb"):
        pass
    with closing(sqlite3.connect(destination, timeout=5)) as backup:
        conn.backup(backup)
        status = backup.execute("PRAGMA integrity_check").fetchone()[0]
        if status != "ok":
            raise sqlite3.DatabaseError("Deterministic rescore backup integrity check failed")
    return destination


def backfill(conn, scorer, *, apply=False, backup_dir=None):
    """Compute every stale active-profile score, gate on non-regression, then atomically save."""
    user = store.profile(conn)
    if user is None:
        raise ValueError("No active profile is available for deterministic rescoring")
    rows = conn.execute("""SELECT s.job_id,s.fit_score prior_score,j.title,j.raw_json,j.department,j.location,
        c.name company_name FROM scores s JOIN jobs j ON j.id=s.job_id
        JOIN companies c ON c.id=j.company_id WHERE s.user_id=? AND s.is_stale=1
        AND j.is_active=1 AND c.active=1 ORDER BY j.id""", (user["id"],)).fetchall()
    if not rows:
        return {"rows": 0, "scores_at_least_prior": 0, "applied": False, "backup": None}

    computed = []
    for row in rows:
        job = {key: row[key] for key in ("job_id", "title", "raw_json", "department", "location")}
        job["id"] = job.pop("job_id")
        result = pipeline.score_job_deterministic(user, job, row["company_name"], scorer)
        if result["fit_score"] < row["prior_score"]:
            raise ValueError("Deterministic rescore did not meet the saved-score non-regression gate")
        computed.append((row, result))

    backup_path = None
    if apply:
        backup_path = _backup(conn, backup_dir or ROOT / "data" / "backups")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            current_user = conn.execute("SELECT resume_text FROM users WHERE id=? AND active=1",
                                         (user["id"],)).fetchone()
            if current_user is None or current_user[0] != user["resume_text"]:
                raise RuntimeError("Profile changed during deterministic rescoring; results were not saved")
            for row, result in computed:
                encoded = json.dumps(result["deterministic_breakdown"], ensure_ascii=False,
                                     sort_keys=True, allow_nan=False)
                cursor = conn.execute("""UPDATE scores SET fit_score=?,reasoning=?,matched_skills=?,
                    missing_requirements=?,scored_at=?,is_stale=0,evaluation_json=NULL,deterministic_json=?
                    WHERE user_id=? AND job_id=? AND is_stale=1 AND fit_score=? AND EXISTS(
                        SELECT 1 FROM jobs j JOIN companies c ON c.id=j.company_id
                        WHERE j.id=scores.job_id AND j.is_active=1 AND c.active=1
                        AND j.title=? AND j.raw_json=? AND j.department IS ? AND j.location IS ?
                        AND c.name=?)""", (result["fit_score"], result["reasoning"],
                    json.dumps(result["matched_skills"], ensure_ascii=False),
                    json.dumps(result["missing_requirements"], ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(), encoded, user["id"], row["job_id"],
                    row["prior_score"], row["title"], row["raw_json"], row["department"], row["location"],
                    row["company_name"]))
                if cursor.rowcount != 1:
                    raise RuntimeError("Score inputs changed during rescoring; batch was not saved")
    return {"rows": len(computed), "scores_at_least_prior": len(computed), "applied": apply,
            "backup": str(backup_path) if backup_path else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="save results after the non-regression gate")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    from backend.local_embeddings import LocalScorer
    database = Path(pipeline.DB_PATH)
    with closing(sqlite3.connect(database, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        if "deterministic_json" not in {row[1] for row in conn.execute("PRAGMA table_info(scores)")}:
            raise RuntimeError("Apply the deterministic score breakdown migration before rescoring")
        scorer = LocalScorer()
        try:
            summary = backfill(conn, scorer, apply=args.apply)
        finally:
            scorer.close()
    logger.info("Deterministic rescore: rows=%s passed_non_regression=%s applied=%s backup=%s",
                summary["rows"], summary["scores_at_least_prior"], summary["applied"],
                "created" if summary["backup"] else "not_needed")
    if not args.apply:
        logger.info("Preview only. Add --apply to save this fully passing batch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
