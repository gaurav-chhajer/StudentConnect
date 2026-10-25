"""Local relevance rankings, deliberately separate from verified fit evaluations."""

import hashlib
import json
import math
import re

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
SCORING_VERSION = "similarity-v1"
PAGE_SIZE = 20
# Mention matching only: a skill name is not proof of proficiency or a requirement.
SKILLS = {
    "Python": ("python",), "Java": ("java",), "JavaScript": ("javascript",),
    "TypeScript": ("typescript",), "C++": ("c++",), "C#": ("c#",),
    ".NET": (".net", "dotnet"), "SQL": ("sql",), "PostgreSQL": ("postgresql", "postgres"),
    "MySQL": ("mysql",), "MongoDB": ("mongodb",), "Redis": ("redis",),
    "HTML": ("html",), "CSS": ("css",), "React": ("react", "reactjs", "react.js"),
    "Angular": ("angular",), "Vue": ("vue", "vue.js"), "Node.js": ("node.js", "nodejs"),
    "Flask": ("flask",), "Django": ("django",), "FastAPI": ("fastapi",),
    "Spring": ("spring", "spring boot"), "Git": ("git",), "Linux": ("linux",),
    "Docker": ("docker",), "Kubernetes": ("kubernetes", "k8s"),
    "AWS": ("aws", "amazon web services"), "Azure": ("azure",),
    "Google Cloud": ("google cloud", "gcp"), "Excel": ("excel",),
    "Power BI": ("power bi",), "Tableau": ("tableau",), "Pandas": ("pandas",),
    "NumPy": ("numpy",), "PyTorch": ("pytorch",), "TensorFlow": ("tensorflow",),
    "Machine learning": ("machine learning",), "Data analysis": ("data analysis",),
    "Statistics": ("statistics",), "REST": ("rest", "restful"),
    "Go": ("golang",), "Rust": ("rust",), "Swift": ("swift",), "Kotlin": ("kotlin",),
    "Accounting": ("accounting",), "Financial modelling": ("financial modelling", "financial modeling"),
    "Sales": ("sales",), "Marketing": ("marketing",), "Figma": ("figma",),
}


def text_hash(text):
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise TypeError("Hashed content must be text or null")
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def job_hash(job):
    value = {key: job[key] for key in ("title", "department", "location")}
    value["raw_json"] = json.loads(job["raw_json"])
    return text_hash(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def skills(text):
    lowered = text.casefold()
    return sorted(name for name, aliases in SKILLS.items()
                  if any(re.search(r"(?<![\w+#.])" + re.escape(alias) + r"(?![\w+#])", lowered) for alias in aliases))


def skill_comparison(resume, description):
    return compare_skills(skills(resume), skills(description))


def compare_skills(resume_skills, jd_skills):
    resume_skills, jd_skills = set(resume_skills), set(jd_skills)
    matched = sorted(resume_skills & jd_skills)
    return matched, sorted(jd_skills - resume_skills), len(matched) / len(jd_skills) if jd_skills else 0.0


def ranking(cosine, overlap, has_jd_skills):
    semantic = max(0.0, cosine)
    # No recognized JD skills means no skill evidence, rather than full credit.
    return round(100 * (0.7 * semantic + 0.3 * overlap if has_jd_skills else semantic), 4)


def result(cosine, resume, description):
    return result_from_skills(cosine, skills(resume), skills(description))


def result_from_skills(cosine, resume_skills, jd_skills):
    if type(cosine) not in (int, float) or not math.isfinite(cosine) or not -1 <= cosine <= 1:
        raise ValueError("Cosine similarity must be finite and between -1 and 1")
    matched, absent, overlap = compare_skills(resume_skills, jd_skills)
    return dict(model_revision=MODEL_REVISION, scoring_version=SCORING_VERSION,
                cosine=float(cosine), skill_overlap=overlap,
                rank_score=ranking(cosine, overlap, bool(matched or absent)),
                matched_skills=matched, jd_skills_not_mentioned=absent)


def validate_result(value, resume, description):
    if not isinstance(value, dict) or set(value) != set(result(0, resume, description)):
        raise ValueError("Invalid local similarity fields")
    expected = result(value.get("cosine"), resume, description)
    if any(type(value[key]) is bool for key in ("cosine", "skill_overlap", "rank_score")) or value != expected:
        raise ValueError("Local similarity does not match its version or skill evidence")
    return expected


def register_sql(conn):
    conn.create_function("sc_text_hash", 1, text_hash, deterministic=True)
    conn.create_function("sc_job_hash", 4, lambda title, department, location, raw:
                         job_hash(dict(title=title, department=department, location=location, raw_json=raw)), deterministic=True)


def current_where():
    return """FROM local_similarity s JOIN users u ON u.id=s.user_id
        JOIN jobs j ON j.id=s.job_id JOIN companies c ON c.id=j.company_id
        WHERE s.user_id=? AND u.active=1 AND j.is_active=1 AND c.active=1
        AND s.resume_hash=sc_text_hash(u.resume_text)
        AND s.preferences_hash=sc_text_hash(u.preferences_json)
        AND s.job_hash=sc_job_hash(j.title,j.department,j.location,j.raw_json)
        AND s.model_revision=? AND s.scoring_version=?
        AND (NOT EXISTS(SELECT 1 FROM user_settings WHERE user_id=u.id AND subscriptions_configured=1)
             OR EXISTS(SELECT 1 FROM company_subscriptions WHERE user_id=u.id AND company_id=c.id))"""


def matches(conn, user_id, page):
    register_sql(conn)
    params = (user_id, MODEL_REVISION, SCORING_VERSION)
    count = conn.execute("SELECT COUNT(*) " + current_where(), params).fetchone()[0]
    rows = conn.execute("""SELECT s.*,j.title,j.url,j.location,c.name company_name """ + current_where() +
                        " ORDER BY s.rank_score DESC,s.job_id LIMIT ? OFFSET ?",
                        (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        for field in ("matched_skills", "jd_skills_not_mentioned"):
            item[field] = json.loads(item[field])
        items.append(item)
    return items, max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
