"""Local-only section embeddings and conservative fuzzy skill fallback."""

import math

from backend import matching_extraction as extraction
from backend.local_embeddings import normalize


_SECTION_WEIGHT = {
    "requirements": 1.0,
    "nice_to_have": 0.75,
    "responsibilities": 1.0,
    "experience": 1.0,
    "projects": 0.9,
    "skills": 0.9,
    "education": 0.75,
    "summary": 0.75,
    "other": 0.8,
    "certifications": 0.75,
}
FUZZY_SKILL_THRESHOLD = 0.92


def _cosine(left, right):
    a, b = normalize(left), normalize(right)
    if len(a) != len(b):
        raise ValueError("Embedding dimensions do not match")
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def section_similarity(resume_text, jd_text, scorer, *, resume_owner, jd_owner):
    """Compare each JD section to its best resume section using cached local vectors."""
    if not isinstance(resume_owner, str) or not resume_owner.strip() or len(resume_owner) > 200:
        raise ValueError("Resume embedding owner must be bounded text")
    if not isinstance(jd_owner, str) or not jd_owner.strip() or len(jd_owner) > 200:
        raise ValueError("JD embedding owner must be bounded text")
    resume_sections = extraction.split_sections(resume_text)
    jd_sections = extraction.split_sections(jd_text)
    resume_vectors = []
    for index, section in enumerate(resume_sections):
        if not section["text"].strip():
            continue
        owner = f"{resume_owner}:section:{index}:{section['name']}"
        vector, _ = scorer.embedding(section["text"], owner)
        resume_vectors.append(vector)
    if not resume_vectors:
        raise ValueError("Resume has no embeddable sections")

    weighted_scores = []
    for index, section in enumerate(jd_sections):
        if not section["text"].strip():
            continue
        owner = f"{jd_owner}:section:{index}:{section['name']}"
        jd_vector, _ = scorer.embedding(section["text"], owner)
        best = max(_cosine(jd_vector, vector) for vector in resume_vectors)
        weight = _SECTION_WEIGHT.get(section["name"], 0.8)
        weighted_scores.append((max(0.0, best), weight))
    if not weighted_scores:
        raise ValueError("Job description has no embeddable sections")
    total_weight = sum(weight for _, weight in weighted_scores)
    return round(sum(value * weight for value, weight in weighted_scores) / total_weight, 6)


def select_fuzzy_matches(resume_skills, jd_skills, pair_cosines, *, threshold=FUZZY_SKILL_THRESHOLD):
    """Choose one-to-one high-confidence skill pairs, excluding known confusions."""
    if (not isinstance(resume_skills, (list, tuple)) or not isinstance(jd_skills, (list, tuple)) or
            any(not isinstance(skill, str) or skill not in extraction.SKILLS for skill in (*resume_skills, *jd_skills))):
        raise ValueError("Fuzzy matching requires recognized taxonomy skills")
    if (type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 < threshold <= 1):
        raise ValueError("Fuzzy threshold must be finite and between zero and one")
    if not isinstance(pair_cosines, dict):
        raise ValueError("Fuzzy similarities must be a pair mapping")
    candidates = []
    for jd_skill in sorted(set(jd_skills)):
        for resume_skill in sorted(set(resume_skills)):
            if jd_skill == resume_skill:
                continue
            if frozenset((jd_skill, resume_skill)) in extraction.BLOCKED_SKILL_PAIRS:
                continue
            value = pair_cosines.get((jd_skill, resume_skill))
            if value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or not -1 <= value <= 1:
                raise ValueError("Fuzzy similarity must be finite and between -1 and one")
            if value >= threshold:
                candidates.append((float(value), jd_skill, resume_skill))
    selected = []
    used_resume, used_jd = set(), set()
    for value, jd_skill, resume_skill in sorted(candidates, reverse=True):
        if jd_skill not in used_jd and resume_skill not in used_resume:
            selected.append({"jd_skill": jd_skill, "resume_skill": resume_skill,
                             "cosine": round(value, 6), "type": "fuzzy"})
            used_jd.add(jd_skill)
            used_resume.add(resume_skill)
    return sorted(selected, key=lambda item: (item["jd_skill"], item["resume_skill"]))


def fuzzy_skill_matches(resume_mentions, jd_mentions, scorer, *, threshold=FUZZY_SKILL_THRESHOLD):
    """Embed only unmatched recognized skill labels and apply the blocked high threshold."""
    resume_skills = sorted({item["skill"] for item in resume_mentions})
    jd_skills = sorted({item["skill"] for item in jd_mentions})
    exact_or_implied = set(resume_skills) & set(jd_skills)
    resume_skills = [skill for skill in resume_skills if skill not in exact_or_implied]
    jd_skills = [skill for skill in jd_skills if skill not in exact_or_implied]
    vectors = {}
    for skill in sorted(set(resume_skills) | set(jd_skills)):
        vectors[skill] = scorer.embedding(skill, "skill-taxonomy:deterministic-v1")[0]
    pair_cosines = {
        (jd_skill, resume_skill): _cosine(vectors[jd_skill], vectors[resume_skill])
        for jd_skill in jd_skills for resume_skill in resume_skills
    }
    return select_fuzzy_matches(resume_skills, jd_skills, pair_cosines, threshold=threshold)


def fuzzy_unknown_skill_matches(resume_text, jd_text, scorer, *, resume_owner, jd_owner,
                                threshold=FUZZY_SKILL_THRESHOLD):
    """Compare unknown JD requirement terms to raw phrases in the resume skills section."""
    if (not isinstance(resume_owner, str) or not resume_owner.strip() or len(resume_owner) > 200 or
            not isinstance(jd_owner, str) or not jd_owner.strip() or len(jd_owner) > 200):
        raise ValueError("Fuzzy matching owners must be bounded text")
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ValueError("Fuzzy threshold must be finite and between zero and one")
    requirements = extraction.extract_jd_requirements(jd_text)
    unknown_jd = sorted({skill for item in requirements
                         if item["priority"] in {"must_have", "nice_to_have", "inferred_must_have"}
                         for skill in item["unknown_skills"]}, key=str.casefold)[:100]
    resume_phrases = extraction.extract_resume_skill_phrases(resume_text)
    if not unknown_jd or not resume_phrases:
        return []
    vectors = {}
    for skill in unknown_jd:
        vectors[("jd", skill)] = scorer.embedding(skill, "jd-unknown-skill:" + jd_owner)[0]
    for item in resume_phrases:
        phrase = item["phrase"]
        vectors[("resume", phrase)] = scorer.embedding(
            phrase, "resume-skill-phrases:" + resume_owner)[0]
    candidates = []
    for jd_skill in unknown_jd:
        for item in resume_phrases:
            resume_skill = item["phrase"]
            score = (1.0 if extraction.normalize_text(jd_skill) == extraction.normalize_text(resume_skill)
                     else _cosine(vectors[("jd", jd_skill)], vectors[("resume", resume_skill)]))
            if score >= threshold:
                candidates.append((score, jd_skill, resume_skill))
    selected = []
    used_jd, used_resume = set(), set()
    for score, jd_skill, resume_skill in sorted(candidates, reverse=True):
        if jd_skill not in used_jd and resume_skill not in used_resume:
            selected.append({"jd_skill": jd_skill, "resume_skill": resume_skill,
                             "cosine": round(score, 6), "type": "fuzzy"})
            used_jd.add(jd_skill)
            used_resume.add(resume_skill)
    return sorted(selected, key=lambda item: (item["jd_skill"].casefold(), item["resume_skill"].casefold()))


def propose_skill_aliases(candidates, scorer, *, threshold=0.92, minimum_margin=0.03):
    """Suggest high-confidence unknown JD terms for human taxonomy review."""
    if not isinstance(candidates, list) or len(candidates) > 1000:
        raise ValueError("Alias review candidates must be a bounded list")
    if (type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 < threshold <= 1 or
            type(minimum_margin) not in (int, float) or not math.isfinite(minimum_margin) or
            not 0 <= minimum_margin < 1):
        raise ValueError("Alias proposal thresholds are invalid")
    known_aliases = {extraction.normalize_text(alias) for aliases in extraction.SKILLS.values() for alias in aliases}
    proposals = []
    for item in candidates:
        if (not isinstance(item, dict) or set(item) != {"skill", "count", "first_seen", "last_seen"} or
                not isinstance(item["skill"], str) or not item["skill"].strip() or len(item["skill"]) > 100 or
                type(item["count"]) is not int or item["count"] < 1 or
                extraction.normalize_text(item["skill"]) in known_aliases):
            raise ValueError("Invalid unknown-skill review candidate")
        candidate = item["skill"].strip()
        candidate_vector = scorer.embedding(candidate, "alias-review:deterministic-v1")[0]
        similarities = []
        for skill, aliases in extraction.SKILLS.items():
            score = max(_cosine(candidate_vector,
                                scorer.embedding(alias, "alias-review:deterministic-v1")[0])
                        for alias in aliases)
            similarities.append((score, skill))
        similarities.sort(reverse=True)
        best_score, best_skill = similarities[0]
        second_score = similarities[1][0] if len(similarities) > 1 else -1.0
        if best_score >= threshold and best_score - second_score >= minimum_margin:
            proposals.append({"alias": candidate, "canonical_skill": best_skill,
                              "cosine": round(best_score, 6), "count": item["count"]})
    return sorted(proposals, key=lambda item: (-item["count"], -item["cosine"], item["alias"].casefold()))
