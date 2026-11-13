import unittest

from backend.public_dataset_eval import (
    ats_score_metrics,
    human_ranking_metrics,
    parse_ats_pair,
    parse_human_rankings,
)


def _annotation_text():
    rows = "\n".join("    [1, 2, 3, 4, 5]," for _ in range(30))
    return ("ANNOTATOR_1_RANKINGS = [\n" + rows + "\n]\n"
            "ANNOTATOR_2_RANKINGS = [\n" + rows + "\n]\n")


class PublicDatasetEvalTests(unittest.TestCase):
    def test_literal_unknown_label_is_not_resubmitted_as_fuzzy(self):
        from unittest.mock import patch
        from backend import public_dataset_eval
        with patch("backend.semantic_similarity.section_similarity", return_value=.5), \
                patch("backend.semantic_similarity.fuzzy_skill_matches", return_value=[]), \
                patch("backend.semantic_similarity.fuzzy_unknown_skill_matches", return_value=[
                    dict(jd_skill="Scrum", resume_skill="SCRUM", cosine=1.0, type="fuzzy")]):
            result = public_dataset_eval.score_pair("Skills\nSCRUM", "Requirements\nScrum", object(), owner="test")
        self.assertEqual(result["missing_must_have"], [])
        self.assertFalse(any(item["type"] == "fuzzy" for item in result["matched"]))

    def test_implied_match_is_not_resubmitted_as_fuzzy(self):
        from unittest.mock import patch
        from backend import public_dataset_eval
        with patch("backend.semantic_similarity.section_similarity", return_value=.5), \
                patch("backend.semantic_similarity.fuzzy_skill_matches", return_value=[
                    dict(jd_skill="Python", resume_skill="Flask", cosine=.95, type="fuzzy")]), \
                patch("backend.semantic_similarity.fuzzy_unknown_skill_matches", return_value=[]):
            result = public_dataset_eval.score_pair("Skills\nFlask", "Requirements\nPython", object(), owner="test")
        self.assertEqual(result["matched"][0]["type"], "implies")

    def test_below_threshold_fuzzy_evidence_still_fails_validation(self):
        from unittest.mock import patch
        from backend import public_dataset_eval
        with patch("backend.semantic_similarity.section_similarity", return_value=.5), \
                patch("backend.semantic_similarity.fuzzy_skill_matches", return_value=[
                    dict(jd_skill="Python", resume_skill="Java", cosine=.5, type="fuzzy")]), \
                patch("backend.semantic_similarity.fuzzy_unknown_skill_matches", return_value=[]):
            with self.assertRaises(ValueError):
                public_dataset_eval.score_pair("Skills\nJava", "Requirements\nPython", object(), owner="test")

    def test_parses_documented_ats_delimiter_and_rejects_ambiguous_rows(self):
        resume, job, score, label = parse_ats_pair("resume text SEP job text", 64.5, "Potential Fit")
        self.assertEqual((resume, job, score, label), ("resume text", "job text", 64.5, "Potential Fit"))
        for row in ("resume text [SEP] job text", "resume SEP job SEP extra", " SEP job"):
            with self.assertRaises(ValueError):
                parse_ats_pair(row, 60, "Potential Fit")
        with self.assertRaises(ValueError):
            parse_ats_pair("resume SEP job", float("nan"), "Potential Fit")

    def test_ats_metrics_are_marked_weak_and_measure_errors_and_reference_cutoff(self):
        metrics = ats_score_metrics([30, 65, 80], [30, 50, 80],
                                    ["No Fit", "Potential Fit", "Good Fit"])
        self.assertEqual(metrics["spearman"], 1.0)
        self.assertEqual(metrics["mean_absolute_error"], 5.0)
        self.assertEqual(metrics["reference_60_threshold_flips"], 1)
        self.assertEqual(metrics["reference_score_label_consistency"], 1.0)
        self.assertFalse(metrics["human_cutoff_calibration"])

    def test_parses_both_annotators_and_allows_documented_rank_ties(self):
        text = _annotation_text().replace("[1, 2, 3, 4, 5]", "[1, 1, 3, 4, 5]", 1)
        rankings = parse_human_rankings(text)
        self.assertEqual(len(rankings["ANNOTATOR_1_RANKINGS"]), 30)
        self.assertEqual(rankings["ANNOTATOR_1_RANKINGS"][0], [1, 1, 3, 4, 5])

    def test_rejects_malformed_or_executable_annotation_content(self):
        with self.assertRaises(ValueError):
            parse_human_rankings(_annotation_text().replace("[1, 2, 3, 4, 5]", "__import__('os')"))
        with self.assertRaises(ValueError):
            parse_human_rankings("x" * 100001)

    def test_ranking_metrics_include_inter_annotator_baseline_not_cutoff_claim(self):
        first = [[1, 2, 3, 4, 5] for _ in range(30)]
        second = [[5, 4, 3, 2, 1] for _ in range(30)]
        metrics = human_ranking_metrics([[5, 4, 3, 2, 1] for _ in range(30)], {
            "ANNOTATOR_1_RANKINGS": first, "ANNOTATOR_2_RANKINGS": second},
            component_scores={"skill": [[5, 4, 3, 2, 1] for _ in range(30)]})
        self.assertEqual(metrics["pairs"], 150)
        self.assertEqual(metrics["annotator_spearman"]["ANNOTATOR_1_RANKINGS"], 1.0)
        self.assertEqual(metrics["annotator_spearman"]["ANNOTATOR_2_RANKINGS"], -1.0)
        self.assertEqual(metrics["inter_annotator_spearman"], -1.0)
        self.assertEqual(metrics["component_spearman"]["skill"], 0.0)
        self.assertFalse(metrics["numeric_cutoff_calibration"])

    def test_rejects_wrong_prediction_shape_and_nonfinite_scores(self):
        rankings = {"ANNOTATOR_1_RANKINGS": [[1, 2, 3, 4, 5]] * 30,
                    "ANNOTATOR_2_RANKINGS": [[1, 2, 3, 4, 5]] * 30}
        with self.assertRaises(ValueError):
            human_ranking_metrics([[1, 2, 3, 4, 5]], rankings)
        with self.assertRaises(ValueError):
            human_ranking_metrics([[float("nan")] * 5] * 30, rankings)
        rankings["ANNOTATOR_2_RANKINGS"][0] = [0, 2, 3, 4, 5]
        with self.assertRaises(ValueError):
            human_ranking_metrics([[1, 2, 3, 4, 5]] * 30, rankings)


if __name__ == "__main__":
    unittest.main()
