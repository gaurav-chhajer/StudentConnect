"""Validate Groq's student matching rubric without replacing its judgments."""

COMPONENT_LIMITS = dict(skills_match=40, experience_relevance=25, role_alignment=15,
                        education_fit=10, other_requirements=10)
RUBRIC_VERSION = "student-v1"


def validate_evaluation(result, resume=None, jd=None):
    """Reject malformed scores and, when supplied, unsupported evidence quotes."""
    if not isinstance(result, dict):
        raise ValueError("Evaluation must be a JSON object")
    if result.get("status") == "unable_to_evaluate":
        raise ValueError("Groq could not evaluate these documents; no score saved")
    if result.get("status") != "evaluated":
        raise ValueError("Evaluation status must be evaluated")
    components = result.get("components")
    if not isinstance(components, dict) or set(components) != set(COMPONENT_LIMITS):
        raise ValueError("Evaluation must include all five rubric components")

    def text(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 4000:
            raise ValueError("Evaluation text must be nonempty and bounded")

    def evidence(value, source, field):
        text(value)
        if source is not None and value not in source:
            raise ValueError(f"Evaluation {field} must quote its source exactly; paraphrases are not accepted")

    for name, maximum in COMPONENT_LIMITS.items():
        component = components[name]
        if not isinstance(component, dict):
            raise ValueError("Invalid rubric component")
        score = component.get("score")
        if type(score) is not int or not 0 <= score <= maximum:
            raise ValueError("Component score is outside its rubric range")
        text(component.get("explanation"))
    text(result.get("reasoning"))
    for field in ("matched_skills", "missing_requirements", "unknown_eligibility"):
        values = result.get(field)
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError("Evaluation lists must be bounded arrays")
        for value in values:
            text(value)
    for field in ("matched_requirements", "hard_blockers"):
        values = result.get(field)
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError("Evidence lists must be bounded arrays")
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise ValueError("Requirement evidence must be an object")
            text(value.get("requirement"))
            evidence(value.get("jd_evidence"), jd, f"{field}[{index}].jd_evidence")
            evidence(value.get("resume_evidence"), resume, f"{field}[{index}].resume_evidence")
    raw = sum(value["score"] for value in components.values())
    fit = min(raw, 40) if result["hard_blockers"] else raw
    for field, expected in (("raw_score", raw), ("fit_score", fit)):
        if type(result.get(field)) is not int or result[field] != expected:
            raise ValueError("Evaluation total or blocker cap is inconsistent")
    category = next(label for minimum, label in
                    ((80, "Strong Match"), (65, "Good Match"), (45, "Partial Match"),
                     (25, "Weak Match"), (0, "Not a Match")) if fit >= minimum)
    if result.get("match_category") != category:
        raise ValueError("Groq match category does not match its score")
    return result
