"""Compare deterministic shadow results with a private legacy-score snapshot."""

import json
import math
from pathlib import Path


def _load_jsonl(path, *, score_field="score"):
    rows = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        score = row.get(score_field) if isinstance(row, dict) else None
        if (not isinstance(row, dict) or type(row.get("job_id")) not in (str, int) or
                type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 100):
            raise ValueError(f"Invalid evaluation row at line {line_number}")
        rows.append(row)
    if not rows or len(rows) > 1000 or len({str(row["job_id"]) for row in rows}) != len(rows):
        raise ValueError("Evaluation input must contain 1 to 1000 unique job rows")
    return rows


def _rank(values):
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    offset = 0
    while offset < len(order):
        end = offset + 1
        while end < len(order) and values[order[end]] == values[order[offset]]:
            end += 1
        rank = (offset + 1 + end) / 2
        for item in order[offset:end]:
            ranks[item] = rank
        offset = end
    return ranks


def _spearman(left, right):
    left_rank, right_rank = _rank(left), _rank(right)
    left_mean = sum(left_rank) / len(left_rank)
    right_mean = sum(right_rank) / len(right_rank)
    covariance = sum((a - left_mean) * (b - right_mean) for a, b in zip(left_rank, right_rank))
    left_variance = sum((value - left_mean) ** 2 for value in left_rank)
    right_variance = sum((value - right_mean) ** 2 for value in right_rank)
    return covariance / (left_variance * right_variance) ** 0.5 if left_variance and right_variance else 0.0


def write_label_template(candidate_path, output_path):
    """Create an unscored human-review file without replacing existing labels."""
    rows = _load_jsonl(candidate_path)
    destination = Path(output_path)
    content = "".join(json.dumps({"job_id": row["job_id"], "human_score": None},
                                  sort_keys=True) + "\n" for row in rows)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    return len(rows)


def compare_to_human(candidate_path, human_labels_path, *, threshold=60):
    """Report calibration and cutoff metrics for one score variant against reviewed labels."""
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 100:
        raise ValueError("Evaluation threshold must be between zero and 100")
    candidate = {str(row["job_id"]): row["score"] for row in _load_jsonl(candidate_path)}
    labels = {str(row["job_id"]): row["human_score"]
              for row in _load_jsonl(human_labels_path, score_field="human_score")}
    if labels.keys() != candidate.keys():
        raise ValueError("Human labels and candidate must contain the same jobs")
    ids = sorted(candidate)
    predicted, human = [candidate[item] for item in ids], [labels[item] for item in ids]
    errors = [actual - expected for actual, expected in zip(predicted, human)]
    flips = sum((actual >= threshold) != (expected >= threshold)
                for actual, expected in zip(predicted, human))
    return {"pairs": len(ids), "spearman": round(_spearman(human, predicted), 4),
            "mean_absolute_error": round(sum(abs(error) for error in errors) / len(errors), 2),
            "mean_signed_error": round(sum(errors) / len(errors), 2), "threshold": threshold,
            "threshold_flips": flips, "threshold_flip_rate": round(flips / len(ids), 4),
            "human_evaluation_ready": len(ids) >= 30}


def compare(baseline_path, candidate_path, human_labels_path=None, *, threshold=60):
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 100:
        raise ValueError("Evaluation threshold must be between zero and 100")
    baseline = {str(row["job_id"]): row for row in _load_jsonl(baseline_path)}
    candidate = {str(row["job_id"]): row for row in _load_jsonl(candidate_path)}
    if baseline.keys() != candidate.keys():
        raise ValueError("Baseline and candidate must contain the same jobs")
    ids = sorted(baseline)
    old = [baseline[item]["score"] for item in ids]
    new = [candidate[item]["score"] for item in ids]
    blocker_hits = blocker_total = 0
    for item in ids:
        expected = baseline[item].get("hard_blockers", [])
        actual = candidate[item].get("missing_must_have_requirements", [])
        blocker_total += len(expected)
        actual_lines = {entry.get("jd_line") for entry in actual if isinstance(entry, dict)}
        blocker_hits += sum(entry.get("jd_line_id") in actual_lines for entry in expected if isinstance(entry, dict))
    result = {
        "pairs": len(ids), "reference_type": "legacy_model_scores",
        "mean_legacy_reference": round(sum(old) / len(old), 2), "mean_candidate": round(sum(new) / len(new), 2),
        "legacy_rank_correlation": round(_spearman(old, new), 4),
        "hard_blockers_detected": blocker_hits, "hard_blockers_total": blocker_total,
        "human_label_pairs": 0, "human_spearman": None, "threshold": threshold,
        "threshold_flips": None, "threshold_flip_rate": None, "human_evaluation_ready": False,
    }
    if human_labels_path is not None:
        human_metrics = compare_to_human(candidate_path, human_labels_path, threshold=threshold)
        result.update(human_label_pairs=human_metrics["pairs"],
                      human_spearman=human_metrics["spearman"],
                      human_mean_absolute_error=human_metrics["mean_absolute_error"],
                      human_mean_signed_error=human_metrics["mean_signed_error"],
                      threshold_flips=human_metrics["threshold_flips"],
                      threshold_flip_rate=human_metrics["threshold_flip_rate"],
                      human_evaluation_ready=human_metrics["human_evaluation_ready"])
    else:
        result.update(human_mean_absolute_error=None, human_mean_signed_error=None)
    return result
