"""Utilities for ranking-only evaluation against public human annotations."""

import ast
import math
import re
from datetime import date

from backend import deterministic_scoring, local_embeddings, semantic_similarity
from backend.deterministic_eval import _spearman


def parse_ats_pair(text, score, label):
    """Split a documented ATS pair row and validate its weak reference values."""
    if not isinstance(text, str) or not text.strip() or len(text) > 250000 or text.count(" SEP ") != 1:
        raise ValueError("ATS dataset text must contain exactly one documented ' SEP ' delimiter")
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError("ATS reference score must be finite and between zero and 100")
    if label not in {"No Fit", "Potential Fit", "Good Fit"}:
        raise ValueError("ATS reference label is invalid")
    resume, job = (part.strip() for part in text.split(" SEP ", 1))
    if not resume or not job:
        raise ValueError("ATS dataset resume and job text must both be nonempty")
    return resume, job, float(score), label


def score_pair(resume_text, jd_text, scorer, *, owner, job_title=""):
    """Run the app's deterministic scoring engine for one local text pair."""
    if (not isinstance(owner, str) or not owner.strip() or len(owner) > 200 or
            not isinstance(resume_text, str) or not isinstance(jd_text, str) or
            not isinstance(job_title, str) or len(job_title) > 500):
        raise ValueError("Scoring pair inputs are invalid")
    resume_hash = local_embeddings.similarity.text_hash(resume_text)
    jd_hash = local_embeddings.similarity.text_hash(jd_text)
    semantic = semantic_similarity.section_similarity(
        resume_text, jd_text, scorer,
        resume_owner=f"{owner}:resume:{resume_hash}", jd_owner=f"{owner}:jd:{jd_hash}")
    resume_mentions = deterministic_scoring.extraction.extract_skill_mentions(resume_text)
    jd_mentions = deterministic_scoring.extraction.extract_skill_mentions(jd_text)
    fuzzy_pairs = semantic_similarity.fuzzy_skill_matches(resume_mentions, jd_mentions, scorer)
    fuzzy_pairs.extend(semantic_similarity.fuzzy_unknown_skill_matches(
        resume_text, jd_text, scorer, resume_owner=f"{owner}:resume:{resume_hash}",
        jd_owner=f"{owner}:jd:{jd_hash}"))
    eligible_requirements = deterministic_scoring.extraction.extract_jd_requirements(jd_text)
    eligible_jd = {skill for item in eligible_requirements
                   if item["priority"] in {"must_have", "nice_to_have", "inferred_must_have"}
                   for skill in (*item["skills"], *item["unknown_skills"])}
    matched_jd = {item["skill"] for item in deterministic_scoring.extraction.expand_implied_skills(resume_mentions)
                  if item["evidence_credit"] > 0}
    # The scorer already credits literal unknown labels before validating fuzzy
    # matches. Case-only variants must not be sent back as new fuzzy evidence.
    unknown_labels = {skill for item in eligible_requirements for skill in item["unknown_skills"]}
    for skill in unknown_labels:
        pattern = deterministic_scoring.extraction._skill_pattern(skill)
        for section in deterministic_scoring.extraction.split_sections(resume_text):
            if section["name"] not in {"skills", "experience", "projects"}:
                continue
            if any(pattern.search(line) and not re.search(r"\b(?:no experience|never|learning|beginner)\b", line, re.I)
                   for line in section["text"].splitlines()):
                matched_jd.add(skill)
    accepted_pairs = []
    used_resume_terms = set()
    accepted_jd_terms = set()
    for pair in fuzzy_pairs:
        jd_key = pair["jd_skill"].casefold()
        resume_key = pair["resume_skill"].casefold()
        if (pair["jd_skill"] in eligible_jd and pair["resume_skill"] != pair["jd_skill"] and
                jd_key not in {skill.casefold() for skill in matched_jd} and
                jd_key not in accepted_jd_terms and resume_key not in used_resume_terms):
            accepted_pairs.append(pair)
            matched_jd.add(pair["jd_skill"])
            accepted_jd_terms.add(jd_key)
            used_resume_terms.add(resume_key)
    return deterministic_scoring.score(
        resume_text, jd_text, semantic, job_title=job_title,
        as_of_date=date.today(), fuzzy_skill_pairs=accepted_pairs)


def ats_score_metrics(predicted, reference, labels):
    """Compare scores to the dataset's algorithmic reference, not human truth."""
    if (not isinstance(predicted, list) or not predicted or len(predicted) > 10000 or
            any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100
                for value in predicted) or not isinstance(reference, list) or
            len(reference) != len(predicted) or
            any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100
                for value in reference) or not isinstance(labels, list) or len(labels) != len(predicted) or
            any(value not in {"No Fit", "Potential Fit", "Good Fit"} for value in labels)):
        raise ValueError("ATS evaluation vectors must contain matching valid scores and labels")
    errors = [actual - expected for expected, actual in zip(predicted, reference)]
    threshold_flips = sum((actual >= 60) != (expected >= 60)
                          for expected, actual in zip(predicted, reference))
    tier_agreement = sum(("No Fit" if score < 40 else "Potential Fit" if score <= 70 else "Good Fit") == label
                         for score, label in zip(predicted, labels))
    reference_tier_consistency = sum(
        ("No Fit" if score < 40 else "Potential Fit" if score <= 70 else "Good Fit") == label
        for score, label in zip(reference, labels))
    return {
        "pairs": len(predicted),
        "reference_type": "algorithmic_ats_scores_not_human_labels",
        "spearman": round(_spearman(reference, predicted), 4),
        "mean_absolute_error": round(sum(abs(error) for error in errors) / len(errors), 2),
        "mean_signed_error": round(sum(errors) / len(errors), 2),
        "reference_60_threshold_flips": threshold_flips,
        "reference_60_threshold_flip_rate": round(threshold_flips / len(predicted), 4),
        "reference_tier_agreement": round(tier_agreement / len(predicted), 4),
        "reference_score_label_consistency": round(reference_tier_consistency / len(predicted), 4),
        "human_cutoff_calibration": False,
    }


def parse_human_rankings(annotation_text):
    """Parse the two documented annotator matrices without evaluating code."""
    if not isinstance(annotation_text, str) or len(annotation_text) > 100000:
        raise ValueError("Annotation input must be bounded text")
    parsed = {}
    lines = annotation_text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(ANNOTATOR_[12]_RANKINGS)\s*=", line)
        if not match:
            continue
        key = match.group(1)
        if key in parsed:
            raise ValueError("Duplicate annotator ranking")
        block = []
        bracket_balance = 0
        for current in lines[index:]:
            if block and re.match(r"^ANNOTATOR_[12]_RANKINGS\s*=", current):
                break
            current = current.split("#", 1)[0]
            block.append(current)
            if "=" in current:
                current = current.split("=", 1)[1]
            bracket_balance += current.count("[") - current.count("]")
            if block and bracket_balance == 0:
                break
        expression = "\n".join(block).split("=", 1)[1].strip().rstrip(",")
        try:
            matrix = ast.literal_eval(expression)
        except (SyntaxError, ValueError) as error:
            raise ValueError("Invalid annotator ranking matrix") from error
        if (not isinstance(matrix, list) or len(matrix) != 30 or
                any(not isinstance(row, list) or len(row) != 5 or
                    any(type(value) is not int or not 1 <= value <= 5 for value in row)
                    for row in matrix)):
            raise ValueError("Each annotator must provide 30 rows of five ranks from one to five")
        parsed[key] = matrix
    if set(parsed) != {"ANNOTATOR_1_RANKINGS", "ANNOTATOR_2_RANKINGS"}:
        raise ValueError("Both annotator ranking matrices are required")
    return parsed


def human_ranking_metrics(scores, rankings, *, component_scores=None):
    """Compare each resume's five predicted scores with both annotators' ranks."""
    if (not isinstance(scores, list) or len(scores) != 30 or
            any(not isinstance(row, list) or len(row) != 5 or
                any(type(value) not in (int, float) or not math.isfinite(value) for value in row)
                for row in scores)):
        raise ValueError("Predicted scores must be a finite 30 by 5 matrix")
    if not isinstance(rankings, dict) or set(rankings) != {
            "ANNOTATOR_1_RANKINGS", "ANNOTATOR_2_RANKINGS"}:
        raise ValueError("Both annotator ranking matrices are required")
    if any(not isinstance(matrix, list) or len(matrix) != 30 or
           any(not isinstance(row, list) or len(row) != 5 or
               any(type(value) is not int or not 1 <= value <= 5 for value in row)
               for row in matrix) for matrix in rankings.values()):
        raise ValueError("Annotator rankings must be 30 by 5 with ranks from one to five")
    if component_scores is not None and (not isinstance(component_scores, dict) or
            any(not isinstance(matrix, list) or len(matrix) != 30 or
                any(not isinstance(row, list) or len(row) != 5 or
                    any(type(value) not in (int, float) or not math.isfinite(value) for value in row)
                    for row in matrix) for matrix in component_scores.values())):
        raise ValueError("Each component must be a finite 30 by 5 score matrix")
    correlations = {key: [] for key in rankings}
    inter_annotator = []
    for resume_index, predicted in enumerate(scores):
        first = rankings["ANNOTATOR_1_RANKINGS"][resume_index]
        second = rankings["ANNOTATOR_2_RANKINGS"][resume_index]
        inter_annotator.append(_spearman(first, second))
        for key in rankings:
            best_first = [-value for value in rankings[key][resume_index]]
            correlations[key].append(_spearman(best_first, predicted))
    mean = lambda values: round(sum(values) / len(values), 4)
    return {
        "pairs": 150,
        "resume_count": 30,
        "job_count": 5,
        "annotator_spearman": {key: mean(values) for key, values in correlations.items()},
        "component_spearman": {
            component: mean([
                _spearman([-value for value in rankings[annotator][resume_index]], matrix[resume_index])
                for annotator in rankings for resume_index in range(30)])
            for component, matrix in (component_scores or {}).items()},
        "mean_annotator_spearman": mean([value for values in correlations.values() for value in values]),
        "inter_annotator_spearman": mean(inter_annotator),
        "benchmark_type": "human_rankings_only",
        "numeric_cutoff_calibration": False,
    }
