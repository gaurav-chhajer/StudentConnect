import json
import tempfile
import unittest
from pathlib import Path

from backend.deterministic_eval import compare, compare_to_human, write_label_template


class DeterministicEvalTests(unittest.TestCase):
    def test_legacy_comparison_is_diagnostic_not_a_score_quality_gate(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            baseline = Path(directory) / "baseline.jsonl"
            candidate = Path(directory) / "candidate.jsonl"
            baseline.write_text("\n".join(json.dumps(row) for row in [
                {"job_id": 1, "score": 20, "hard_blockers": [{"jd_line_id": 4}]},
                {"job_id": 2, "score": 60, "hard_blockers": []},
            ]), encoding="utf-8")
            candidate.write_text("\n".join(json.dumps(row) for row in [
                {"job_id": 1, "score": 25, "missing_must_have_requirements": [{"jd_line": 4}]},
                {"job_id": 2, "score": 60, "missing_must_have_requirements": []},
            ]), encoding="utf-8")
            metrics = compare(baseline, candidate)
        self.assertEqual(metrics["reference_type"], "legacy_model_scores")
        self.assertNotIn("all_scores_at_least_baseline", metrics)
        self.assertFalse(metrics["human_evaluation_ready"])
        self.assertIsNone(metrics["human_spearman"])
        self.assertEqual(metrics["hard_blockers_detected"], 1)
        self.assertEqual(metrics["hard_blockers_total"], 1)
        self.assertEqual(metrics["legacy_rank_correlation"], 1.0)

    def test_human_labels_measure_rank_agreement_and_threshold_flips(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            baseline = Path(directory) / "baseline.jsonl"
            candidate = Path(directory) / "candidate.jsonl"
            labels = Path(directory) / "labels.jsonl"
            baseline.write_text('{"job_id":1,"score":20}\n{"job_id":2,"score":60}', encoding="utf-8")
            candidate.write_text('{"job_id":1,"score":25}\n{"job_id":2,"score":60}', encoding="utf-8")
            labels.write_text('{"job_id":1,"human_score":70}\n{"job_id":2,"human_score":60}',
                              encoding="utf-8")
            metrics = compare(baseline, candidate, labels)
        self.assertEqual(metrics["human_spearman"], -1.0)
        self.assertEqual(metrics["threshold_flips"], 1)
        self.assertEqual(metrics["threshold_flip_rate"], 0.5)
        self.assertEqual(metrics["human_mean_absolute_error"], 22.5)
        self.assertEqual(metrics["human_mean_signed_error"], -22.5)
        self.assertFalse(metrics["human_evaluation_ready"])

    def test_label_template_is_unscored_and_never_overwrites(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            candidate = Path(directory) / "candidate.jsonl"
            template = Path(directory) / "labels.jsonl"
            candidate.write_text('{"job_id":1,"score":25}\n{"job_id":2,"score":60}',
                                 encoding="utf-8")
            self.assertEqual(write_label_template(candidate, template), 2)
            self.assertEqual(template.read_text(encoding="utf-8").splitlines(), [
                '{"human_score": null, "job_id": 1}', '{"human_score": null, "job_id": 2}'])
            with self.assertRaises(FileExistsError):
                write_label_template(candidate, template)

    def test_ablation_compares_a_variant_to_human_labels(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            candidate = Path(directory) / "candidate.jsonl"
            labels = Path(directory) / "labels.jsonl"
            candidate.write_text('{"job_id":1,"score":58}\n{"job_id":2,"score":72}',
                                 encoding="utf-8")
            labels.write_text('{"job_id":1,"human_score":61}\n{"job_id":2,"human_score":70}',
                              encoding="utf-8")
            metrics = compare_to_human(candidate, labels)
        self.assertEqual(metrics["mean_absolute_error"], 2.5)
        self.assertEqual(metrics["mean_signed_error"], -0.5)
        self.assertEqual(metrics["threshold_flips"], 1)
        self.assertEqual(metrics["threshold_flip_rate"], 0.5)
        self.assertEqual(metrics["spearman"], 1.0)

    def test_requires_same_unique_pair_set(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            baseline = Path(directory) / "baseline.jsonl"
            candidate = Path(directory) / "candidate.jsonl"
            baseline.write_text('{"job_id":1,"score":20}', encoding="utf-8")
            candidate.write_text('{"job_id":2,"score":20}', encoding="utf-8")
            with self.assertRaises(ValueError):
                compare(baseline, candidate)

    def test_rejects_invalid_human_labels_and_thresholds(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            baseline = Path(directory) / "baseline.jsonl"
            candidate = Path(directory) / "candidate.jsonl"
            labels = Path(directory) / "labels.jsonl"
            baseline.write_text('{"job_id":1,"score":20}', encoding="utf-8")
            candidate.write_text('{"job_id":1,"score":25}', encoding="utf-8")
            labels.write_text('{"job_id":1,"human_score":101}', encoding="utf-8")
            with self.assertRaises(ValueError):
                compare(baseline, candidate, labels)
            with self.assertRaises(ValueError):
                compare(baseline, candidate, threshold=101)


if __name__ == "__main__":
    unittest.main()
