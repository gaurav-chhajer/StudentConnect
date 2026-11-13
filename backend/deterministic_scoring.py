"""Pure deterministic fit scoring; embedding similarity is supplied by the caller."""

import json
import math
import re
from numbers import Real
from datetime import date

from backend import matching_extraction as extraction


SCORER_VERSION = "deterministic-v4"
WEIGHTS_VERSION = "weights-v1"
WEIGHTS = {
    "must_have": 40,
    "nice_to_have": 10,
    "semantic": 25,
    "experience": 15,
    "education_title": 10,
}
MISSING_MUST_HAVE_FRACTION = 0.5
MISSING_MUST_HAVE_SCORE_CAP = 60
NEUTRAL_COMPONENT = 0.7
UNOBSERVED_SKILL_COMPONENT = 0.4
_DEGREE_LEVEL = {"diploma": 1, "bachelors": 2, "masters": 3, "doctorate": 4}
_SENIORITY_LEVEL = {"intern": 0, "entry": 1, "mid": 2, "senior": 3, "lead": 4, "management": 5}


def _fraction(value):
    return round(min(1.0, max(0.0, value)), 4)


def cache_version(resume_text, as_of_date, embedding_revision):
    """Build a cache key that only changes daily for active experience ranges."""
    if type(as_of_date) is not date or not isinstance(embedding_revision, str) or not embedding_revision.strip():
        raise ValueError("Cache version inputs are invalid")
    date_key = as_of_date.isoformat() if extraction.has_current_experience_date(resume_text) else "static"
    return ":".join((SCORER_VERSION, WEIGHTS_VERSION, "coverage-v8-monotone", extraction.TAXONOMY_VERSION,
                     embedding_revision, date_key))


def template_explanation(result):
    """Describe only the evidence and gaps already present in a deterministic result."""
    if not isinstance(result, dict) or type(result.get("score")) is not int:
        raise ValueError("A valid deterministic result is required")
    matched = result.get("matched", [])
    gaps = result.get("missing_must_have_requirements", [])
    missing_skills = result.get("missing_must_have", [])
    parts = [f"Deterministic fit score: {result['score']}/100.",
             f"{len(matched)} evidence-backed match(es)."]
    coverage = result.get("requirement_coverage")
    if isinstance(coverage, dict):
        parts.append(f"Recognized {coverage['recognized']} of {coverage['total']} requirement clauses; "
                     "unrecognized clauses need manual review.")
    if gaps:
        parts.append(f"{len(gaps)} required gap(s) are listed below.")
    elif missing_skills:
        parts.append("Required skill gaps are listed below.")
    else:
        parts.append("No gaps among recognized requirements; unrecognized requirements still need review.")
    return " ".join(parts)


def _coverage(skills, resume_mentions, *, inferred=()):
    if not skills:
        return UNOBSERVED_SKILL_COMPONENT
    inferred = set(inferred)
    credits = []
    for skill in skills:
        if skill not in resume_mentions:
            credits.append(0.0)
            continue
        mention = resume_mentions[skill]
        # Presence satisfies a skill criterion; inferred/fuzzy evidence and
        # unsupported keyword lists retain their conservative credit limits.
        credit = mention["evidence_credit"]
        if mention["match_type"] not in {"implies", "fuzzy"} and credit >= 0.75:
            credit = 1.0
        credits.append(min(credit, 0.4) if skill in inferred else credit)
    return _fraction(sum(credits) / len(skills))


def _education_title_fit(resume_text, jd_text, job_title):
    jd_education = extraction.extract_education_evidence(jd_text)
    resume_education = extraction.extract_education_evidence(resume_text)
    if jd_education:
        education_fit = 1.0 if any(_education_evidence_matches(required, actual)
                                   for required in jd_education for actual in resume_education) else 0.0
    else:
        education_fit = NEUTRAL_COMPONENT

    body = "\n".join(line for line in jd_text.splitlines()
                     if not re.search(r"\b(?:report(?:ing)? to|work with|senior engineers)\b", line, re.I))
    jd_seniority = extraction.extract_seniority(job_title) or extraction.extract_seniority(body)
    resume_seniority = extraction.extract_seniority(resume_text)
    if jd_seniority and resume_seniority:
        requested = max(_SENIORITY_LEVEL[level] for level in jd_seniority)
        actual = max(_SENIORITY_LEVEL[level] for level in resume_seniority)
        title_fit = 1.0 if actual >= requested else max(0.0, 1.0 - 0.25 * (requested - actual))
    else:
        title_fit = NEUTRAL_COMPONENT
    return _fraction((education_fit + title_fit) / 2)


def _education_evidence_matches(required, actual):
    if required["levels"]:
        required_level = min(_DEGREE_LEVEL[level] for level in required["levels"])
        actual_level = max((_DEGREE_LEVEL[level] for level in actual["levels"]), default=0)
        if actual_level < required_level:
            return False
    if required["fields"] and not set(required["fields"]) & set(actual["fields"]):
        return False
    return bool(required["levels"] or required["fields"])


def _resume_evidence(resume_text, requirements):
    mentions = {item["skill"]: item for item in extraction.expand_implied_skills(
                extraction.extract_skill_mentions(resume_text)) if item["evidence_credit"] > 0}
    for skill in {skill for item in requirements for skill in item["unknown_skills"]}:
        pattern = extraction._skill_pattern(skill)
        for section in extraction.split_sections(resume_text):
            if section["name"] not in {"skills", "experience", "projects"}:
                continue
            for offset, line in enumerate(section["text"].splitlines()):
                match = pattern.search(line)
                if match and not re.search(r"\b(?:no experience|never|learning|beginner)\b", line, re.I):
                    mentions[skill] = dict(skill=skill, matched_text=match.group(), match_type="alias",
                                          evidence_credit=0.4, section=section["name"], line=section["start_line"] + offset)
    return mentions


def pending_fuzzy_matches(resume_text, jd_text, pairs):
    """Omit already-supported proposals; leave invalid proposals for strict validation."""
    existing = _resume_evidence(resume_text, extraction.extract_jd_requirements(jd_text))
    return [pair for pair in pairs if not (
        isinstance(pair, dict) and set(pair) == {"jd_skill", "resume_skill", "cosine", "type"}
        and isinstance(pair.get("jd_skill"), str) and pair["jd_skill"] in existing
        and isinstance(pair.get("resume_skill"), str) and pair.get("type") == "fuzzy"
        and type(pair.get("cosine")) in (int, float) and math.isfinite(pair["cosine"])
        and extraction.FUZZY_SKILL_THRESHOLD <= pair["cosine"] <= 1)]


def score(resume_text, jd_text, semantic_similarity, *, job_title="", as_of_date, fuzzy_skill_pairs=()):
    """Calculate a versioned score from bounded text and a local cosine value."""
    for label, value in (("resume", resume_text), ("job description", jd_text)):
        if not isinstance(value, str) or not value.strip() or len(value) > 250000 or "\x00" in value:
            raise ValueError(f"{label.capitalize()} must be nonempty text within the matching limit")
    if not isinstance(job_title, str) or len(job_title) > 1000:
        raise ValueError("Job title must be bounded text")
    if type(as_of_date) is not date:
        raise TypeError("Scoring date must be a date")
    if (isinstance(semantic_similarity, bool) or not isinstance(semantic_similarity, Real) or not math.isfinite(semantic_similarity)
            or not -1 <= semantic_similarity <= 1):
        raise ValueError("Semantic similarity must be finite and between -1 and 1")
    semantic_similarity = float(semantic_similarity)

    jd_mentions = extraction.extract_skill_mentions(jd_text, all_mentions=True)
    requirements = extraction.extract_jd_requirements(jd_text, job_title=job_title)
    resume_mentions = _resume_evidence(resume_text, requirements)
    # Resolve explicit adjacent alternatives to one criterion; conjunctions remain separate.
    for item in requirements:
        mentions = extraction.extract_skill_mentions(item["text"], all_mentions=True, section_context="requirements")
        spans = []
        for mention in mentions:
            match = re.search(re.escape(mention["matched_text"]), item["text"], re.I)
            if match:
                spans.append((match.start(), match.end(), mention["skill"]))
        spans.sort()
        groups = []
        for start, end, skill in spans:
            if groups and re.fullmatch(r"\s*(?:,?\s*or)\s*", item["text"][groups[-1][-1][1]:start], re.I):
                groups[-1].append((start, end, skill))
            else:
                groups.append([(start, end, skill)])
        for group in groups:
            if len(group) < 2:
                continue
            options = [entry[2] for entry in group]
            selected = max(options, key=lambda skill: resume_mentions.get(skill, {}).get("evidence_credit", 0))
            item["skills"] = [skill for skill in item["skills"] if skill not in options or skill == selected]
    inferred_title_skills = {skill for item in requirements
                             if item["priority"] == "inferred_must_have" and item["line"] == 0
                             for skill in item["skills"]}
    if inferred_title_skills:
        jd_mentions.extend(dict(item, line=0, section="job_title")
                           for item in extraction.extract_skill_mentions(job_title, all_mentions=True)
                           if item["skill"] in inferred_title_skills)
    for requirement in requirements:
        if requirement["priority"] not in {"must_have", "nice_to_have", "inferred_must_have"}:
            continue
        jd_mentions.extend({"skill": skill, "matched_text": skill, "match_type": "unknown",
                            "section": "job_title" if requirement["line"] == 0 else "other",
                            "line": requirement["line"], "evidence_credit": 0.4}
                           for skill in requirement["unknown_skills"])
    if not isinstance(fuzzy_skill_pairs, (list, tuple)):
        raise ValueError("Fuzzy skill matches must be a list")
    jd_skill_set = {item["skill"] for item in jd_mentions}
    explicit_resume_skills = {item["skill"] for item in extraction.extract_skill_mentions(resume_text)}
    resume_skill_phrases = extraction.extract_resume_skill_phrases(resume_text)
    phrase_by_name = {extraction.normalize_text(item["phrase"]): item for item in resume_skill_phrases}
    for pair in fuzzy_skill_pairs:
        if (not isinstance(pair, dict) or set(pair) != {"jd_skill", "resume_skill", "cosine", "type"} or
                pair.get("type") != "fuzzy" or not isinstance(pair.get("jd_skill"), str) or
                pair.get("jd_skill") not in jd_skill_set or not isinstance(pair.get("resume_skill"), str) or
                (pair.get("resume_skill") not in explicit_resume_skills and
                 extraction.normalize_text(pair["resume_skill"]) not in phrase_by_name) or
                pair.get("jd_skill") in resume_mentions or
                pair.get("resume_skill") == pair.get("jd_skill") or
                (pair.get("jd_skill") in extraction.SKILLS and pair.get("resume_skill") in extraction.SKILLS and
                 frozenset((pair.get("jd_skill"), pair.get("resume_skill"))) in extraction.BLOCKED_SKILL_PAIRS) or
                type(pair.get("cosine")) not in (int, float) or not math.isfinite(pair["cosine"]) or
                pair["cosine"] < extraction.FUZZY_SKILL_THRESHOLD or pair["cosine"] > 1):
            raise ValueError("Invalid or below-threshold fuzzy skill match")
        if pair["resume_skill"] in resume_mentions:
            source = resume_mentions[pair["resume_skill"]]
            evidence = dict(source, matched_text=source["matched_text"])
        else:
            source = phrase_by_name[extraction.normalize_text(pair["resume_skill"])]
            evidence = dict(source, matched_text=source["phrase"])
        resume_mentions[pair["jd_skill"]] = dict(evidence, skill=pair["jd_skill"], match_type="fuzzy",
                                                   fuzzy_from=pair["resume_skill"],
                                                   evidence_credit=min(evidence["evidence_credit"], 0.4))
    must_skills = sorted({skill for item in requirements if item["priority"] == "must_have"
                          for skill in item["skills"]})
    must_unknown_skills = sorted({skill for item in requirements if item["priority"] == "must_have"
                                  for skill in item["unknown_skills"]}, key=str.casefold)
    inferred_skills = sorted({skill for item in requirements if item["priority"] == "inferred_must_have"
                              for skill in item["skills"]})
    inferred_unknown_skills = sorted({skill for item in requirements if item["priority"] == "inferred_must_have"
                                      for skill in item["unknown_skills"]}, key=str.casefold)
    nice_skills = sorted({skill for item in requirements if item["priority"] == "nice_to_have"
                          for skill in item["skills"]})
    nice_unknown_skills = sorted({skill for item in requirements if item["priority"] == "nice_to_have"
                                  for skill in item["unknown_skills"]}, key=str.casefold)
    missing = [skill for skill in must_skills + must_unknown_skills if skill not in resume_mentions]
    missing_inferred = [skill for skill in inferred_skills + inferred_unknown_skills if skill not in resume_mentions]
    jd_education = extraction.extract_education_evidence(jd_text)
    resume_education = extraction.extract_education_evidence(resume_text)
    education_by_clause = {item["text"]: extraction.extract_education_evidence(item["text"])
                           for item in requirements}
    education_requirements = [item for item in requirements if item["priority"] == "must_have"
                              and education_by_clause[item["text"]]]
    missing_education = [item for item in education_requirements
                         if not any(_education_evidence_matches(required, candidate)
                                    for required in education_by_clause[item["text"]]
                                    for candidate in resume_education)]
    matched = []
    for jd_mention in jd_mentions:
        skill = jd_mention["skill"]
        if skill not in resume_mentions:
            continue
        resume_mention = resume_mentions[skill]
        if resume_mention["match_type"] in ("implies", "fuzzy"):
            match_type = resume_mention["match_type"]
        else:
            match_type = "exact" if jd_mention["matched_text"].casefold() == resume_mention["matched_text"].casefold() else "alias"
        matched.append({
            "jd": jd_mention["matched_text"],
            "resume": resume_mention["matched_text"],
            "type": match_type,
            "skill": skill,
            "jd_line": jd_mention["line"],
            "resume_line": resume_mention["line"],
        })
    for jd_item in jd_education:
        candidate = next((item for item in resume_education
                          if _education_evidence_matches(jd_item, item)), None)
        if candidate is not None:
            matched.append({"jd": jd_item["text"], "resume": candidate["text"], "type": "education",
                            "jd_line": jd_item["line"], "resume_line": candidate["line"]})

    semantic_fit = _fraction(max(0.0, semantic_similarity))
    year_requirements = [item for item in requirements
                         if item["priority"] == "must_have" and item["minimum_years"] is not None]
    required_years = [item["minimum_years"] for item in year_requirements]
    candidate_years = extraction.experience_years(resume_text, as_of_date) if required_years else None
    per_skill_years = extraction.skill_experience_years(resume_text, as_of_date) if required_years else {}

    def observed_years(item):
        if item["skills"]:
            return min((per_skill_years.get(skill, 0.0) for skill in item["skills"]), default=0.0)
        return candidate_years

    missing_year_requirements = [item for item in year_requirements
                                 if observed_years(item) < item["minimum_years"]]
    if required_years:
        ratios = [1.0 if item["minimum_years"] == 0 else
                  min(1.0, observed_years(item) / item["minimum_years"])
                  for item in year_requirements]
        experience_fit = _fraction(sum(ratios) / len(ratios))
    else:
        demonstrated = [resume_mentions[skill]["evidence_credit"]
                        for skill in set(must_skills + must_unknown_skills + nice_skills + nice_unknown_skills)
                        if skill in resume_mentions and resume_mentions[skill]["section"] in ("experience", "projects")]
        # Additional weaker evidence must not dilute existing evidence or the
        # established neutral baseline when no duration is requested.
        experience_fit = _fraction(max([NEUTRAL_COMPONENT, *demonstrated]))

    components = {
        "must_have": _coverage(must_skills + must_unknown_skills + inferred_skills + inferred_unknown_skills,
                                resume_mentions, inferred=inferred_skills + inferred_unknown_skills),
        "nice_to_have": _coverage(nice_skills + nice_unknown_skills, resume_mentions),
        "semantic": semantic_fit,
        "experience": experience_fit,
        "education_title": _education_title_fit(resume_text, jd_text, job_title),
    }
    component_points = {name: round(components[name] * maximum, 2) for name, maximum in WEIGHTS.items()}
    raw_score = sum(component_points.values())
    must_have_count = len(must_skills) + len(must_unknown_skills) + len(required_years) + len(education_requirements)
    missing_count = len(missing) + len(missing_year_requirements) + len(missing_education)
    missing_fraction = missing_count / must_have_count if must_have_count else 0.0
    jd_levels = extraction.extract_seniority(job_title)
    resume_levels = extraction.extract_seniority(resume_text)
    seniority_gap = bool(jd_levels and resume_levels and
                         max(_SENIORITY_LEVEL[x] for x in jd_levels) > max(_SENIORITY_LEVEL[x] for x in resume_levels))
    if seniority_gap:
        must_have_count += 1
        missing_count += 1
        missing_fraction = missing_count / must_have_count
    capped = bool((must_have_count and missing_fraction > MISSING_MUST_HAVE_FRACTION)
                  or missing_year_requirements or seniority_gap)
    final_score = round(raw_score)
    if capped:
        final_score = min(final_score, MISSING_MUST_HAVE_SCORE_CAP)
    missing_requirements = [
        {"jd": item["text"], "jd_line": item["line"],
         "reason": "experience_years" if item["minimum_years"] is not None else "missing_skill"}
        for item in requirements
        if item["priority"] == "must_have" and (
            item in missing_year_requirements or
            item in missing_education or
            any(skill in missing for skill in item["skills"] + item["unknown_skills"]))
    ]
    if seniority_gap:
        missing_requirements.append({"jd": job_title, "jd_line": 0, "reason": "seniority"})
    assessed = [item for item in requirements if item["priority"] in {"must_have", "nice_to_have", "inferred_must_have"}]
    recognized = [item for item in assessed if item["skills"] or item["minimum_years"] is not None
                  or education_by_clause.get(item["text"])]
    recognized_texts = {item["text"] for item in recognized}
    return {
        "score": final_score,
        "raw_score": round(raw_score, 2),
        "components": components,
        "component_points": component_points,
        "weights": dict(WEIGHTS),
        "requirement_coverage": {"total": len(assessed), "recognized": len(recognized),
                                 "ratio": round(len(recognized) / len(assessed), 4) if assessed else 0.0,
                                 "needs_review": [item["text"][:200] for item in assessed if item["text"] not in recognized_texts][:100]},
        "matched": matched,
        "missing_must_have": missing,
        "missing_inferred_skills": missing_inferred,
        "missing_must_have_requirements": missing_requirements,
        "must_have_skills": must_skills,
        "must_have_unknown_skills": must_unknown_skills,
        "inferred_must_have_skills": inferred_skills + inferred_unknown_skills,
        "nice_to_have_skills": nice_skills,
        "nice_to_have_unknown_skills": nice_unknown_skills,
        "experience_evidence": [
            {"jd": item["text"], "jd_line": item["line"], "required_years": item["minimum_years"],
             "observed_years": observed_years(item), "skills": item["skills"]}
            for item in year_requirements
        ],
        "missing_must_have_fraction": round(missing_fraction, 4),
        "must_have_criterion_count": must_have_count,
        "missing_must_have_criterion_count": missing_count,
        "hard_cap_applied": capped,
        "experience_hard_cap_applied": bool(missing_year_requirements or seniority_gap),
        "scorer_version": SCORER_VERSION,
        "weights_version": WEIGHTS_VERSION,
        "scored_as_of": as_of_date.isoformat(),
    }


def validate_adapted_result(result):
    """Validate a deterministic worker score before it enters the shared score table."""
    from backend.store import _deterministic_breakdown

    if isinstance(result, dict) and isinstance(result.get("missing_requirements"), list):
        result = dict(result, missing_requirements=[item[:200] if isinstance(item, str) else item
                                                  for item in result["missing_requirements"]])

    if (not isinstance(result, dict) or type(result.get("fit_score")) is not int or
            not 0 <= result["fit_score"] <= 100 or
            not isinstance(result.get("reasoning"), str) or not result["reasoning"].strip() or
            len(result["reasoning"]) > 2000 or
            any(not isinstance(result.get(key), list) or len(result[key]) > 100 or
                any(not isinstance(item, str) or not item.strip() or len(item) > 200
                    for item in result[key]) for key in ("matched_skills", "missing_requirements")) or
            not isinstance(result.get("deterministic_breakdown"), dict)):
        raise ValueError("Invalid deterministic worker score")
    breakdown = _deterministic_breakdown(json.dumps(result["deterministic_breakdown"], allow_nan=False))
    if (breakdown["score"] != result["fit_score"] or
            breakdown.get("scorer_version") != SCORER_VERSION or
            breakdown.get("weights_version") != WEIGHTS_VERSION or
            not isinstance(result.get("scorer_version"), str) or
            result["scorer_version"] != SCORER_VERSION or
            result.get("weights_version") != WEIGHTS_VERSION):
        raise ValueError("Deterministic worker score versions do not match")
    expected_matches = sorted({item["skill"] for item in breakdown["matched"] if item.get("skill")})
    expected_missing = list(breakdown["missing_must_have"])
    for item in breakdown["missing_must_have_requirements"]:
        if item["jd"] not in expected_missing:
            expected_missing.append(item["jd"])
    if result["matched_skills"] != expected_matches[:100] or result["missing_requirements"] != [item[:200] for item in expected_missing[:100]]:
        raise ValueError("Deterministic worker summary does not match its score breakdown")
    return result
