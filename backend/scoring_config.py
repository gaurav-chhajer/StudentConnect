"""Plain configuration for the opt-in deterministic student-v2 scoring engine."""

from typing import TypedDict


RUBRIC_VERSION_V2 = "student-v2"
EXTRACTOR_VERSION = "ext-1"

COMPONENT_MAX = {
    "must_have_skills": 30,
    "experience_relevance": 20,
    "role_domain_fit": 15,
    "impact": 10,
    "education_fit": 10,
    "nice_to_have": 10,
    "logistics": 5,
}
EVIDENCE_CREDIT = {
    "not_found": 0.0,
    "listed_only": 0.4,
    "used_in_project_or_role": 0.75,
    "used_with_outcome": 1.0,
}
# Age <= 2 years uses within_2y; age > 2 and <= 5 uses 2_to_5y.
RECENCY = {"within_2y": 1.0, "2_to_5y": 0.7, "over_5y": 0.4, "unknown": 0.85}
NEUTRAL_DEFAULT = 0.7
RELEVANCE_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.2, "none": 0.0}
DOMAIN_RELATION = {"same": 1.0, "adjacent": 0.6, "unrelated": 0.2}
BANDS = (
    (80, "Strong Match"),
    (65, "Good Match"),
    (45, "Partial Match"),
    (25, "Weak Match"),
    (0, "Not a Match"),
)
GATE_FAIL_CAP = 40
PENALTY_UNSUPPORTED_CLAIMS = 5
CONFIDENCE_REVIEW_THRESHOLD = 0.5


class ConfigResult(TypedDict):
    """Result of validating scoring constants without raising to the caller."""

    ok: bool
    value: bool | None
    error: str | None


def validate_config() -> ConfigResult:
    """Check positive integer component maxima total 100 and bands strictly descend."""
    try:
        if not isinstance(COMPONENT_MAX, dict) or not COMPONENT_MAX:
            return {"ok": False, "value": None, "error": "Component maxima must be a nonempty mapping"}
        for maximum in COMPONENT_MAX.values():
            if type(maximum) is not int or maximum <= 0:
                return {"ok": False, "value": None, "error": "Component maxima must be positive integers"}
        if sum(COMPONENT_MAX.values()) != 100:
            return {"ok": False, "value": None, "error": "Component maxima must sum to 100"}
        if not isinstance(BANDS, (tuple, list)) or not BANDS:
            return {"ok": False, "value": None, "error": "Score bands must be nonempty"}
        previous_minimum = 101
        for band in BANDS:
            if not isinstance(band, (tuple, list)) or len(band) != 2:
                return {"ok": False, "value": None, "error": "Score bands must contain a minimum and label"}
            minimum, label = band
            if type(minimum) is not int or not 0 <= minimum <= 100:
                return {"ok": False, "value": None, "error": "Band minima must be integers from 0 to 100"}
            if not isinstance(label, str) or not label.strip():
                return {"ok": False, "value": None, "error": "Band labels must be nonempty strings"}
            if minimum >= previous_minimum:
                return {"ok": False, "value": None, "error": "Score bands must strictly descend"}
            previous_minimum = minimum
        if previous_minimum != 0:
            return {"ok": False, "value": None, "error": "Score bands must include a zero minimum"}
        return {"ok": True, "value": True, "error": None}
    except (TypeError, ValueError, AttributeError, OverflowError):
        return {"ok": False, "value": None, "error": "Scoring configuration could not be validated"}
