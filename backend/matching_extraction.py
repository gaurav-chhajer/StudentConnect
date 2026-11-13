"""Deterministic, section-aware resume and job-description extraction."""

import re
import json
import unicodedata
from datetime import date
from pathlib import Path

_TAXONOMY_PATH = Path(__file__).parent / "taxonomy" / "skills.json"
_TAXONOMY = json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))
if (not isinstance(_TAXONOMY, dict) or set(_TAXONOMY) !=
        {"taxonomy_version", "source", "skills", "implies", "blocked_pairs"} or
        not isinstance(_TAXONOMY["taxonomy_version"], str) or
        not isinstance(_TAXONOMY["skills"], dict) or not _TAXONOMY["skills"]):
    raise ValueError("Invalid versioned skill taxonomy")
SKILLS = {name: tuple(aliases) for name, aliases in _TAXONOMY["skills"].items()}
if (any(not isinstance(name, str) or not name.strip() or not isinstance(aliases, tuple) or not aliases or
        any(not isinstance(alias, str) or not alias.strip() for alias in aliases)
        for name, aliases in SKILLS.items()) or
        not isinstance(_TAXONOMY["implies"], dict) or
        any(source not in SKILLS or not isinstance(targets, list) or
            any(target not in SKILLS for target in targets)
            for source, targets in _TAXONOMY["implies"].items())):
    raise ValueError("Skill taxonomy contains invalid aliases or implications")
TAXONOMY_VERSION = _TAXONOMY["taxonomy_version"]
IMPLIES = {source: tuple(targets) for source, targets in _TAXONOMY["implies"].items()}
BLOCKED_SKILL_PAIRS = frozenset(frozenset(pair) for pair in _TAXONOMY["blocked_pairs"])
FUZZY_SKILL_THRESHOLD = 0.92


_SECTION_NAMES = {
    "summary": ("summary", "profile", "professional summary", "about me", "objective"),
    "skills": ("skills", "technical skills", "core skills", "technical expertise", "competencies",
               "key skills", "skills & tools", "tech stack", "technologies", "core competencies", "technical proficiency"),
    "experience": ("experience", "work experience", "professional experience", "employment history",
                   "work history", "employment", "career history", "relevant experience", "internships",
                   "internship experience", "work experience & internships"),
    "projects": ("projects", "personal projects", "academic projects", "selected projects"),
    "education": ("education", "academic background", "qualifications"),
    "requirements": ("requirements", "qualifications", "key requirements", "required skills", "who you are",
                     "minimum qualifications", "basic qualifications", "must have", "must haves", "what you will need", "what you'll need",
                     "what we are looking for", "what we're looking for", "required qualifications"),
    "responsibilities": ("responsibilities", "what you will do", "what you'll do", "the role",
                          "about the role"),
    "nice_to_have": ("additional experience (not required)", "optional", "optional (not required)", "good to have", "nice-to-have", "preferred", "preferred qualifications", "preferred skills", "nice to have", "bonus points",
                      "additional qualifications"),
    "certifications": ("certifications", "certificates"),
}
_HEADER_LOOKUP = {alias: name for name, aliases in _SECTION_NAMES.items() for alias in aliases}
_MUST_CUE = re.compile(r"\b(?:must|required|mandatory|essential|minimum|at least|need to|needs to)\b", re.I)
_NICE_CUE = re.compile(r"\b(?:preferred|nice to have|bonus|a plus|would be a plus|desirable|ideally)\b", re.I)
_YEAR_RANGE = re.compile(
    r"\b(\d{1,2}(?:\.\d{1,2})?)(?:\s*(?:-|to)\s*\d{1,2}(?:\.\d{1,2})?|\s*\+)?\s*(?:years?|yrs?)\b",
    re.I,
)
_EDUCATION_PATTERNS = (
    ("doctorate", re.compile(r"\b(?:ph\.?d\.?|doctor(?:ate)?|doctoral)\b", re.I)),
    ("masters", re.compile(r"(?<!\w)(?:master'?s|master of|m\.?\s?sc\.?|m\.?e\.?(?=\s+in\b|\s+[A-Z])|m\.?s\.?(?=\s+in\b|\s*\()|m\.?tech|mba|mca|post[ -]?graduate)(?!\w)", re.I)),
    ("bachelors", re.compile(r"(?<!\w)(?:bachelor'?s|bachelor of|b\.?s\.?(?=\s+in\b|\s*\()|b\.?\s?sc\.?|b\.?e\.?(?=\s+in\b|\s+[A-Z])|b\.?tech|bca|bba|b\.?com|undergraduate)(?!\w)", re.I)),
    ("diploma", re.compile(r"\b(?:diploma|associate degree)\b", re.I)),
)
_EDUCATION_FIELDS = {
    "computer_science": re.compile(r"\b(?:computer science|computing)\b", re.I),
    "engineering": re.compile(r"\bengineering\b", re.I),
    "information_technology": re.compile(r"\binformation technology\b", re.I),
    "data_science": re.compile(r"\bdata science\b", re.I),
    "mathematics": re.compile(r"\b(?:mathematics|statistics)\b", re.I),
    "business": re.compile(r"\b(?:business|management|commerce|finance|accounting)\b", re.I),
}
_INFORMATION_TECHNOLOGY_ABBREVIATION = re.compile(r"(?<![A-Za-z])IT(?![A-Za-z])")
_BTECH = re.compile(r"\b(?:b\.?tech|m\.?tech|bachelor of technology|master of technology)\b", re.I)
_SENIORITY_PATTERNS = (
    ("intern", re.compile(r"\bintern(?:ship)?\b", re.I)),
    ("entry", re.compile(r"\b(?:entry[ -]?level|graduate|fresher|junior|student)\b", re.I)),
    ("mid", re.compile(r"\b(?:mid[ -]?level|intermediate)\b", re.I)),
    ("senior", re.compile(r"\bsenior\b", re.I)),
    ("lead", re.compile(r"\b(?:lead|principal|staff)\b", re.I)),
    ("management", re.compile(r"\b(?:manager|director|head of|vice president)\b", re.I)),
)
_SECTION_CREDIT = {"experience": 0.75, "projects": 0.75, "skills": 0.4, "summary": 0.4}
_WORK_ACTION = re.compile(r"\b(?:built|developed|implemented|deployed|created|wrote|designed|maintained|delivered|optimized|automated|reduced|improved|used|using)\b", re.I)
_DATE_PART = r"(?:0?[1-9]|1[0-2])/\d{4}|[A-Za-z]{3,9}\.?\s*'\d{2}|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{4}|\d{4}"
_DATE_RANGE = re.compile(
    r"(?<!\d)(?P<start>" + _DATE_PART + r")\s*(?:-|\u2013|\u2014|to)\s*"
    r"(?P<end>" + _DATE_PART + r"|present|currently|current|now|ongoing|till\s+date|to\s+date|till\s+now)(?!\w)", re.I,
)
_MONTHS = {name: index for index, aliases in enumerate((
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
    ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
    ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december"),
), start=1) for name in aliases}


def normalize_text(value):
    """Normalize Unicode and whitespace without merging distinct skill names."""
    if not isinstance(value, str):
        raise TypeError("Matching text must be a string")
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _section_header(line):
    cleaned = re.sub(r"^[\s#>*\u2022\-]+|[\s:\uff1a]+$", "", line).casefold()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return _HEADER_LOOKUP.get(cleaned)


def split_sections(text):
    """Split recognized headings while retaining original line numbers and text."""
    if not isinstance(text, str) or len(text) > 250000:
        raise ValueError("Document text must be a string within the matching limit")
    sections = []
    current_name = "other"
    current_lines = []
    current_start = 1

    def finish():
        if current_lines:
            sections.append({"name": current_name, "text": "\n".join(current_lines),
                             "start_line": current_start})

    for line_number, line in enumerate(text.splitlines(), start=1):
        header = _section_header(line)
        if header is not None:
            finish()
            current_name = header
            current_lines = []
            current_start = line_number + 1
        else:
            if not current_lines:
                current_start = line_number
            current_lines.append(line)
    finish()
    return sections or [{"name": "other", "text": text, "start_line": 1}]


def _skill_pattern(alias):
    return re.compile(r"(?<![\w+#.])" + re.escape(alias) + r"(?![\w+#])", re.I)


_SKILL_PATTERNS = tuple(
    (skill, alias, _skill_pattern(alias))
    for skill, aliases in SKILLS.items()
    for alias in sorted(set(aliases), key=len, reverse=True)
) + (
    *((skill, alias, _skill_pattern(alias)) for skill, alias in (
        ("Python", "Python3"), ("JavaScript", "JS"), ("TypeScript", "TS"),
        ("Vue", "VueJS"), ("Angular", "AngularJS"), ("HTML", "html5"),
        ("CI/CD", "CICD"), ("MongoDB", "Mongo"), ("Node.js", "Node JS"))),
    ("Go", "Go", re.compile(r"(?<![\w+#.])Go(?![\w+#])", re.I)),
    ("Machine learning", "ML", _skill_pattern("ML")),
    ("Git", "GitHub", _skill_pattern("GitHub")),
    ("CI/CD", "Jenkins pipelines", re.compile(r"\bJenkins\s+pipelines?\b", re.I)),
)
_UNKNOWN_CANDIDATE = re.compile(
    r"\b(?:[A-Z][a-z]{2,}|[A-Z]{2,8}|[A-Z][a-z0-9]*[A-Z][A-Za-z0-9]*|"
    r"[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+|[+#]))\b")
_UNKNOWN_STOPWORDS = {"and", "api", "bachelor", "bachelors", "ceo", "cto", "developer", "engineer",
                      "experience", "for", "from", "has", "have", "hiring", "hr", "html", "including",
                      "jd", "join", "mba", "ms", "must", "of", "or", "preferred", "programmer",
                      "proficiency", "required", "role", "software", "sops", "strong", "team", "usa", "with"}
_UNKNOWN_STOPWORDS.update({"apply", "master", "masters", "computer", "science", "engineering", "education", "knowledge", "familiarity", "minimum", "additional", "proficient", "skills", "tools", "technologies"})
_AMBIGUOUS_SKILLS = frozenset(("Go", "Excel", "REST", "React", "Spring", "Swift", "Room", "Sales", "Marketing"))
_TECHNICAL_CONTEXT = re.compile(
    r"\b(?:developer|engineer|programmer|software|framework|api|database|programming|application|app|"
    r"web|mobile|code|language|library|stack|tool|platform|technology|technical|cloud|"
    r"built|developed|implemented|using|used|proficient|expertise)\b", re.I)
_UNKNOWN_SKILL_CONTEXT = re.compile(
    r"\b(?:software|framework|api|database|programming|code|language|library|stack|tool|platform|"
    r"technology|technical|cloud|web|mobile|developer|engineer|programmer)\b", re.I)
_URL_PATTERN = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_BUSINESS_SKILL_CONTEXT = {
    "Sales": re.compile(r"\b(?:sales (?:strategy|pipeline|operations|process|experience|targets?)|selling)\b", re.I),
    "Marketing": re.compile(r"\b(?:marketing (?:campaigns?|strategy|operations|experience)|digital marketing|content marketing|seo)\b", re.I),
}


def extract_skill_mentions(text, *, all_mentions=False, section_context=None):
    """Return unique skills, or line-level skill evidence when requested."""
    if type(all_mentions) is not bool:
        raise TypeError("all_mentions must be boolean")
    if section_context is not None and section_context not in {*_SECTION_NAMES, "other"}:
        raise ValueError("Unknown skill section context")
    sections = split_sections(text)
    found = {}
    line_mentions = {}
    for section in sections:
        effective_section = section_context or section["name"]
        for line_offset, line in enumerate(section["text"].splitlines()):
            line = " ".join(unicodedata.normalize("NFKC", line).translate(dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u00ad"))).split())
            for skill, alias, pattern in _SKILL_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                if not _ambiguous_skill_match_allowed(skill, alias, match, line, effective_section):
                    continue
                context = line[max(0, match.start() - 70):match.end() + 25]
                if re.search(r"\b(?:no experience|never used|not used|without experience|do not know)\b", line[max(0, match.start() - 70):match.start()], re.I):
                    continue
                candidate = {
                    "skill": skill,
                    "matched_text": match.group(),
                    "match_type": "exact" if alias.casefold() == skill.casefold() else "alias",
                    "section": effective_section,
                    "line": section["start_line"] + line_offset,
                    "evidence_credit": _SECTION_CREDIT.get(section["name"], 0.4),
                }
                if section["name"] in {"experience", "projects"}:
                    if _WORK_ACTION.search(line) and re.search(r"\b\d+(?:\.\d+)?\s*(?:%|percent|users|requests|seconds|ms|hours)\b|\d+%", line, re.I):
                        candidate["evidence_credit"] = 1.0
                    elif not _WORK_ACTION.search(line):
                        if not re.search(r"\b(?:services|scripts|applications|pipelines|systems|apis|deployed|production|project|developer|engineer)\b", line, re.I):
                            candidate["evidence_credit"] = 0.1
                evidence_context = line[max(0, match.start() - 70):match.start()] + " " + line[match.end():match.end() + 25]
                evidence_context = re.sub(r"\bmachine\s+learning\b", "", evidence_context, flags=re.I)
                if re.search(r"\b(?:learning|beginner|familiar|course|aspir(?:e|ing)|want to learn)\b", evidence_context, re.I):
                    candidate["evidence_credit"] = min(candidate["evidence_credit"], 0.1)
                if effective_section in {"requirements", "nice_to_have"}:
                    candidate["evidence_credit"] = 0.0
                line_key = (skill, candidate["line"])
                previous_line = line_mentions.get(line_key)
                if previous_line is None or len(candidate["matched_text"]) > len(previous_line["matched_text"]):
                    line_mentions[line_key] = candidate
                previous = found.get(skill)
                if previous is None or candidate["evidence_credit"] > previous["evidence_credit"]:
                    found[skill] = candidate
    if all_mentions:
        return [line_mentions[key] for key in sorted(line_mentions, key=lambda item: (item[0], item[1]))]
    return [found[skill] for skill in sorted(found)]


def _ambiguous_skill_match_allowed(skill, alias, match, line, section):
    if skill == "Rust" and re.match(r"[- ]proof\b", line[match.end():], re.I):
        return False
    # These labels identify a skill only when the surrounding evidence supports
    # that interpretation; a profile URL or a tool name alone is insufficient.
    if alias == "ML":
        if match.group() != "ML" or re.search(r"\d\s*$", line[:match.start()]):
            return False
        return section in {"skills", "requirements", "nice_to_have"} or bool(re.search(
            r"\b(?:train(?:ed|ing)?|models?|classification|prediction|inference|machine learning)\b", line, re.I))
    if alias == "GitHub":
        if any(url.start() <= match.start() < url.end() for url in _URL_PATTERN.finditer(line)):
            return False
        return bool(re.search(r"\b(?:version control|commits?|branch(?:es|ing)?|pull requests?|merge(?:d|s)?)\b", line, re.I))
    if alias == "Jenkins pipelines":
        return bool(re.search(r"\b(?:builds?|deploy(?:ed|ment|ments)?|delivery|integration|automated|automation|CI/CD)\b", line, re.I))
    if skill not in _AMBIGUOUS_SKILLS:
        return True
    matched = match.group()
    if skill in {"Excel", "React", "Spring", "Swift"} and matched[:1].islower():
        return False
    if skill == "REST" and not (matched.isupper() or alias.casefold() == "restful"):
        return False
    if skill == "Spring" and re.match(r"\s+\d{4}\b", line[match.end():]):
        return False
    if skill == "Room" and re.match(r"\s+(?:to\s+grow|for\s+(?:growth|improvement)|for\s+you)\b", line[match.end():], re.I):
        return False
    if section in {"skills", "requirements", "nice_to_have"}:
        return True
    context = line[max(0, match.start() - 60):min(len(line), match.end() + 60)]
    if skill in _BUSINESS_SKILL_CONTEXT:
        return bool(_BUSINESS_SKILL_CONTEXT[skill].search(context))
    return bool(_TECHNICAL_CONTEXT.search(context))


def _unknown_terms(line):
    known = {normalize_text(alias) for aliases in SKILLS.values() for alias in aliases}
    candidates = set()
    # Unknown proper nouns become scoreable skills only with nearby technical
    # context. This keeps company names, financial acronyms, and URLs from
    # becoming hard gaps just because they occur on a required-qualification line.
    without_urls = _URL_PATTERN.sub(" ", line)
    if without_urls != line:
        return []
    acronym_list = bool(re.match(r"\s*(?:skills|tools|technologies)\s*:", line, re.I))
    unknown_text = without_urls
    for _, _, pattern in _SKILL_PATTERNS:
        unknown_text = pattern.sub(lambda match: " " * len(match.group()), unknown_text)
    for match in _UNKNOWN_CANDIDATE.finditer(unknown_text):
        value = match.group().strip(".,;:()[]{}")
        if (len(value) < 3 or (value.isupper() and not acronym_list) or value.casefold() in _UNKNOWN_STOPWORDS or
                normalize_text(value) in known or value.isdecimal()):
            continue
        context = without_urls[max(0, match.start() - 36):min(len(without_urls), match.end() + 36)]
        if not acronym_list and not _UNKNOWN_SKILL_CONTEXT.search(context) and not re.search(r"\b(?:experience with|proficiency in|knowledge of|skills:)\b", without_urls, re.I) and len(without_urls.split()) > 5:
            continue
        candidates.add(value[:100])
    return sorted(candidates, key=str.casefold)


def unknown_skill_candidates(jd_text):
    """Return bounded unmatched terms from explicit or fallback JD skill requirements."""
    requirements = extract_jd_requirements(jd_text)
    candidates = {skill for item in requirements
                  if item["priority"] in {"must_have", "nice_to_have", "inferred_must_have"}
                  for skill in item["unknown_skills"]}
    return sorted(candidates, key=str.casefold)[:100]


def extract_resume_skill_phrases(resume_text):
    """Extract raw labels from an explicit resume skills section for unknown-term matching."""
    known = {normalize_text(alias) for aliases in SKILLS.values() for alias in aliases}
    phrases = []
    for section in split_sections(resume_text):
        if section["name"] != "skills":
            continue
        for offset, line in enumerate(section["text"].splitlines()):
            line = re.sub(r"^\s*(?:languages?|frameworks?|tools?|platforms?|libraries|databases?|technologies)\s*:",
                          "", line, flags=re.I)
            for part in re.split(r"[,;|/\u2022]|\s+and\s+", line, flags=re.I):
                phrase = part.strip(" \t-*\u2022.")
                normalized = normalize_text(phrase) if phrase else ""
                if (not phrase or len(phrase) > 100 or normalized in known or
                        normalized in _UNKNOWN_STOPWORDS or phrase.isdecimal()):
                    continue
                phrases.append({"phrase": phrase, "line": section["start_line"] + offset,
                                "section": "skills", "evidence_credit": 0.4})
    unique = {}
    for item in phrases:
        unique.setdefault(normalize_text(item["phrase"]), item)
    return [unique[key] for key in sorted(unique)][:100]


def _priority(section, line):
    if re.search(r"\b(?:not required|optional|not mandatory)\b", line, re.I):
        return "nice_to_have"
    if section == "nice_to_have" or _NICE_CUE.search(line):
        return "nice_to_have"
    if section == "requirements" or _MUST_CUE.search(line):
        return "must_have"
    return "unclassified"


def _minimum_years(line):
    if re.search(r"\b(?:years? of age|years? old|in business|next\s+\d+\s+years?)\b", line, re.I):
        return None
    match = _YEAR_RANGE.search(line)
    return _year_value(match.group(1)) if match else None


def _year_value(value):
    years = float(value)
    return int(years) if years.is_integer() else years


_INLINE_JD_HEADING = re.compile(
    r"(?<!\w)(additional experience\s*\(not required\)|optional\s*(?:\(not required\))?|" + "|".join(re.escape(alias) for alias in sorted(
        {alias for section in ("requirements", "nice_to_have", "responsibilities")
         for alias in _SECTION_NAMES[section]}, key=len, reverse=True)) + r")\s*:", re.I)


def _requirement_clauses(line, section):
    """Separate flattened clauses without changing their source line numbers."""
    offset = 0
    for heading in _INLINE_JD_HEADING.finditer(line):
        yield from _sentence_clauses(line[offset:heading.start()], section)
        label = heading.group(1).casefold()
        section = "nice_to_have" if "not required" in label or label.startswith("optional") else _HEADER_LOOKUP[label]
        offset = heading.end()
    yield from _sentence_clauses(line[offset:], section)


def _sentence_clauses(text, section):
    boundary = re.compile(r"[;!?]\s+|(?<=[a-z0-9+#)])\.\s+(?=[A-Z])|\s+-\s+(?=[A-Za-z])|\s+(?=Experience with\b)|,\s+(?=(?:but\s+)?(?:preferred|ideally|nice to have)\b)")
    offset = 0
    for match in boundary.finditer(text):
        if match.group().startswith(".") and re.search(r"\b(?:e\.g|i\.e|etc)$", text[max(0, match.start() - 8):match.start()], re.I):
            continue
        content = text[offset:match.start()]
        if content.strip():
            yield content, section
        offset = match.end()
    if text[offset:].strip():
        yield text[offset:], section


def extract_jd_requirements(text, *, job_title=""):
    """Return explicit requirements, or lighter inferred skills when no skill cues exist."""
    if not isinstance(job_title, str) or len(job_title) > 1000:
        raise ValueError("Job title must be bounded text")
    requirements = []
    for section in split_sections(text):
        clauses = ((offset, content, name)
                   for offset, line in enumerate(section["text"].splitlines())
                   for content, name in _requirement_clauses(line, section["name"]))
        for line_offset, line, clause_section in clauses:
            content = line.strip(" \t-*•")
            if not content:
                continue
            mentions = extract_skill_mentions(content, section_context=clause_section)
            years = _minimum_years(content)
            priority = _priority(clause_section, content)
            if not mentions and years is None and priority == "unclassified":
                continue
            requirements.append({
                "text": content,
                "line": section["start_line"] + line_offset,
                "priority": priority,
                "skills": [mention["skill"] for mention in mentions],
                "unknown_skills": _unknown_terms(content) if priority != "unclassified" else [],
                "minimum_years": years,
            })
    cued_skills = {skill for item in requirements if item["priority"] in {"must_have", "nice_to_have"}
                   for skill in item["skills"] + item["unknown_skills"]}
    if not cued_skills:
        inferred = {skill for item in requirements if item["priority"] == "unclassified"
                    for skill in item["skills"] + _unknown_terms(item["text"])}
        for item in requirements:
            if item["priority"] == "unclassified" and (item["skills"] or _unknown_terms(item["text"])):
                item["priority"] = "inferred_must_have"
                item["unknown_skills"] = _unknown_terms(item["text"])
        for mention in extract_skill_mentions(job_title, all_mentions=True):
            if mention["skill"] in inferred:
                continue
            inferred.add(mention["skill"])
            requirements.append({"text": job_title.strip(), "line": 0, "priority": "inferred_must_have",
                                 "skills": [mention["skill"]], "unknown_skills": [], "minimum_years": None})
        for unknown in _unknown_terms(job_title):
            if unknown in inferred:
                continue
            inferred.add(unknown)
            requirements.append({"text": job_title.strip(), "line": 0, "priority": "inferred_must_have",
                                 "skills": [], "unknown_skills": [unknown], "minimum_years": None})
    return requirements


def extract_education(text):
    """Return degree levels explicitly mentioned in document text."""
    if not isinstance(text, str):
        raise TypeError("Education input must be text")
    return [level for level, pattern in _EDUCATION_PATTERNS if pattern.search(text)]


def extract_education_evidence(text):
    """Return degree-level and field evidence with original line numbers."""
    if not isinstance(text, str):
        raise TypeError("Education input must be text")
    sections = split_sections(text)
    education_sections = [section for section in sections if section["name"] == "education"]
    selected = education_sections or sections
    evidence = []
    for section in selected:
        for offset, line in enumerate(section["text"].splitlines()):
            if re.search(r"\bor equivalent (?:practical |work |professional )?experience\b", line, re.I):
                continue
            levels = [level for level, pattern in _EDUCATION_PATTERNS if pattern.search(line)]
            fields = [field for field, pattern in _EDUCATION_FIELDS.items() if pattern.search(line)]
            if not levels and re.search(r"\b(?:report(?:ing)? to|work with|senior engineers)\b", line, re.I):
                continue
            if _INFORMATION_TECHNOLOGY_ABBREVIATION.search(line):
                fields.append("information_technology")
            if _BTECH.search(line):
                fields.append("engineering")
            if not levels and not fields:
                continue
            evidence.append({"levels": levels, "fields": sorted(set(fields)), "text": line,
                             "line": section["start_line"] + offset})
    return evidence


def expand_implied_skills(mentions):
    """Add only explicitly configured, directional skill implications at partial credit."""
    if not isinstance(mentions, list) or any(not isinstance(item, dict) or item.get("skill") not in SKILLS for item in mentions):
        raise ValueError("Skill mentions must be recognized taxonomy entries")
    expanded = {item["skill"]: dict(item) for item in mentions}
    for source in mentions:
        for implied in IMPLIES.get(source["skill"], ()):
            if implied not in expanded or min(source["evidence_credit"], 0.4) > expanded[implied]["evidence_credit"]:
                expanded[implied] = dict(source, skill=implied, match_type="implies",
                                         implied_from=source["skill"],
                                         evidence_credit=min(source["evidence_credit"], 0.4))
    return [expanded[skill] for skill in sorted(expanded)]


def extract_seniority(text):
    """Return seniority terms explicitly present, from most junior to senior."""
    if not isinstance(text, str):
        raise TypeError("Seniority input must be text")
    return [level for level, pattern in _SENIORITY_PATTERNS if pattern.search(text)]


def explicit_years_experience(text):
    """Return explicitly stated experience durations without inferring date ranges."""
    if not isinstance(text, str):
        raise TypeError("Experience input must be text")
    return [_year_value(match.group(1)) for match in _YEAR_RANGE.finditer(text)]


def _date_month(value, *, ending, as_of_date):
    lowered = value.casefold().strip().rstrip(".")
    numeric = re.fullmatch(r"(0?[1-9]|1[0-2])/(\d{4})", lowered)
    if numeric:
        return int(numeric.group(2)), int(numeric.group(1))
    if re.match(r"(?:present|current|currently|now|ongoing|till|to\s+date)", lowered):
        return as_of_date.year, as_of_date.month
    apostrophe = re.search(r"'(\d{2})$", lowered)
    year_match = re.search(r"\d{4}", lowered) or (apostrophe and re.match(r"(\d{2})", apostrophe.group(1)))
    if year_match is None:
        raise ValueError("Invalid experience date")
    year = int(year_match.group()) if len(year_match.group()) == 4 else 2000 + int(year_match.group())
    month_match = re.match(r"([a-z]+)", lowered)
    month = _MONTHS.get(month_match.group(1)[:3], 12 if ending else 1) if month_match else (12 if ending else 1)
    return year, month


def _merged_months(intervals):
    merged = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def experience_years(text, as_of_date):
    """Estimate total duration from explicit date ranges in the experience section."""
    if type(as_of_date) is not date:
        raise TypeError("Scoring date must be a date")
    intervals = []
    for section in split_sections(text):
        if section["name"] != "experience":
            continue
        for line in section["text"].splitlines():
            for match in _DATE_RANGE.finditer(line):
                start_year, start_month = _date_month(match.group("start"), ending=False, as_of_date=as_of_date)
                end_year, end_month = _date_month(match.group("end"), ending=True, as_of_date=as_of_date)
                if (1950 <= start_year <= as_of_date.year and start_month <= 12 and
                        (end_year, end_month) >= (start_year, start_month) and end_year <= as_of_date.year):
                    intervals.append((start_year * 12 + start_month - 1, end_year * 12 + end_month))
    months = _merged_months(intervals)
    return round(months / 12, 2)


def has_current_experience_date(text):
    """Whether experience duration depends on the current scoring date."""
    for section in split_sections(text):
        if section["name"] != "experience":
            continue
        for line in section["text"].splitlines():
            if any(re.match(r"(?:present|current|currently|now|ongoing|till|to\s+date)", match.group("end").casefold())
                   for match in _DATE_RANGE.finditer(line)):
                return True
    return False


def skill_experience_years(text, as_of_date):
    """Return explicit experience-section duration by recognized skill."""
    if type(as_of_date) is not date:
        raise TypeError("Scoring date must be a date")
    periods = {}
    for section in split_sections(text):
        if section["name"] != "experience":
            continue
        active_intervals = []
        for line in section["text"].splitlines():
            line_intervals = []
            matches = list(_DATE_RANGE.finditer(line))
            if matches:
                for match in matches:
                    start_year, start_month = _date_month(match.group("start"), ending=False, as_of_date=as_of_date)
                    end_year, end_month = _date_month(match.group("end"), ending=True, as_of_date=as_of_date)
                    if (1950 <= start_year <= as_of_date.year and (end_year, end_month) >= (start_year, start_month)
                            and end_year <= as_of_date.year):
                        line_intervals.append((start_year * 12 + start_month - 1, end_year * 12 + end_month))
                active_intervals = line_intervals
            skills = [item["skill"] for item in expand_implied_skills(extract_skill_mentions(line))]
            if not skills:
                continue
            intervals = line_intervals or active_intervals
            for skill in skills:
                periods.setdefault(skill, []).extend(intervals)
    return {skill: round(_merged_months(intervals) / 12, 2)
            for skill, intervals in sorted(periods.items()) if intervals}
