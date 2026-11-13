"""Write a local, review-only report of recurring unknown JD skills and ESCO candidates."""

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path

from backend import deterministic_store, esco_review, local_embeddings

ROOT = Path(__file__).resolve().parents[1]


def build_report(conn, catalog, *, minimum_count=2, limit=100, generated_on=None):
    items = deterministic_store.pending_alias_candidates(conn, minimum_count=minimum_count, limit=limit)
    matched = esco_review.exact_candidates(items, catalog)
    matched_terms = {item["unknown_skill"] for item in matched}
    return {"generated_on": (generated_on or date.today()).isoformat(),
            "review_only": True, "active_taxonomy_modified": False,
            "candidates": matched,
            "unmatched_without_exact_esco_label": [item for item in items if item["skill"] not in matched_terms]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minimum-count", type=int, default=2)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--database", type=Path, default=local_embeddings.CACHE_PATH)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "taxonomy-review")
    args = parser.parse_args(argv)
    if args.minimum_count < 1 or args.limit < 1:
        parser.error("--minimum-count and --limit must be positive")
    if not args.database.is_file():
        parser.error("the alias-review database does not exist yet; run local scoring first")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / f"alias-review-{date.today().isoformat()}.json"
    if report_path.exists():
        raise FileExistsError(f"Weekly alias report already exists: {report_path.name}")
    conn = sqlite3.connect(args.database, timeout=5)
    try:
        deterministic_store.initialize(conn)
        catalog = esco_review.load_catalog()
        report = build_report(conn, catalog, minimum_count=args.minimum_count, limit=args.limit)
    finally:
        conn.close()
    with report_path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
    print(f"Wrote {len(report['candidates'])} ESCO review candidates to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
