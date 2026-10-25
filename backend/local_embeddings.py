"""CPU embeddings and bounded private caches; runtime loading never downloads."""

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from backend import similarity

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "data/models/all-MiniLM-L6-v2" / similarity.MODEL_REVISION
CACHE_PATH = ROOT / "data/local_similarity_cache.db"
MAX_CACHE_ROWS = 50000
MAX_DOCUMENT_CHARACTERS = 251000
CHUNK_TOKENS = 224
CHUNK_OVERLAP = 32
DIMENSIONS = 384


def normalize(vector):
    if (not isinstance(vector, list) or len(vector) != DIMENSIONS or
            any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)):
        raise ValueError("Invalid embedding vector")
    length = math.sqrt(sum(value * value for value in vector))
    if length <= 0:
        raise ValueError("Embedding vector must not be empty")
    return [value / length for value in vector]


def load_model():
    if not (MODEL_DIR / "model.safetensors").is_file():
        raise RuntimeError("Prepare the pinned local model with scripts.prepare_similarity --download first")
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(str(MODEL_DIR), device="cpu", local_files_only=True,
                               trust_remote_code=False, model_kwargs={"use_safetensors": True})


def chunks(text, tokenizer, maximum):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_DOCUMENT_CHARACTERS:
        raise ValueError("Embedding input must be nonempty and within the document limit")
    tokenized = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True, truncation=False)
    offsets = tokenized["offset_mapping"]
    if not offsets:
        raise ValueError("Embedding input contains no usable tokens")
    for start in range(0, len(offsets), CHUNK_TOKENS - CHUNK_OVERLAP):
        end = min(start + CHUNK_TOKENS, len(offsets))
        section = text[offsets[start][0]:offsets[end - 1][1]]
        if len(tokenizer.encode(section, add_special_tokens=True, truncation=False)) > maximum:
            raise ValueError("A document chunk exceeds the model context; no text was silently truncated")
        yield section
        if end == len(offsets):
            break


class LocalScorer:
    """One worker owns the model and SQLite connection, and closes them on exit."""

    def __init__(self, model=None, cache_path=CACHE_PATH):
        self.model = model if model is not None else load_model()
        path = Path(cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=5)
        try:
            with self.conn:
                self.conn.execute("""CREATE TABLE IF NOT EXISTS embeddings (
                    owner TEXT NOT NULL,content_hash TEXT NOT NULL,version TEXT NOT NULL,
                    vector_json TEXT NOT NULL,skills_json TEXT NOT NULL,touched TEXT NOT NULL,
                    PRIMARY KEY(owner,content_hash,version))""")
                self.conn.execute("""CREATE TABLE IF NOT EXISTS results (
                    user_id INTEGER NOT NULL,resume_hash TEXT NOT NULL,jd_hash TEXT NOT NULL,version TEXT NOT NULL,
                    result_json TEXT NOT NULL,touched TEXT NOT NULL,PRIMARY KEY(user_id,resume_hash,jd_hash,version))""")
        except BaseException:
            self.conn.close()
            raise
        self.version = similarity.MODEL_REVISION + ":" + similarity.SCORING_VERSION

    def close(self):
        self.conn.close()

    def embedding(self, text, owner):
        digest = similarity.text_hash(text)
        row = self.conn.execute("SELECT vector_json,skills_json FROM embeddings WHERE owner=? AND content_hash=? AND version=?",
                                (owner, digest, self.version)).fetchone()
        if row:
            known_skills = json.loads(row[1])
            if (not isinstance(known_skills, list) or len(known_skills) > len(similarity.SKILLS) or
                    any(not isinstance(skill, str) or skill not in similarity.SKILLS for skill in known_skills)):
                raise ValueError("Invalid cached skill summary")
            return normalize(json.loads(row[0])), known_skills
        summed, count, batch = [0.0] * DIMENSIONS, 0, []

        def encode_batch(sections):
            vectors = self.model.encode(sections, batch_size=16, normalize_embeddings=True,
                                        convert_to_numpy=True, show_progress_bar=False)
            if len(vectors) != len(sections):
                raise ValueError("Embedding model returned an invalid batch")
            for vector in vectors:
                values = normalize(vector.tolist())
                for index, value in enumerate(values):
                    summed[index] += value

        for section in chunks(text, self.model.tokenizer, self.model.max_seq_length):
            batch.append(section)
            count += 1
            if len(batch) == 16:
                encode_batch(batch)
                batch.clear()
        if batch:
            encode_batch(batch)
        if not count:
            raise ValueError("Document produced no embeddings")
        vector = normalize(summed)
        known_skills = similarity.skills(text)
        with self.conn:
            self.conn.execute("INSERT INTO embeddings VALUES(?,?,?,?,?,?)",
                              (owner, digest, self.version, json.dumps(vector, allow_nan=False),
                               json.dumps(known_skills), datetime.now(timezone.utc).isoformat()))
            self.conn.execute("DELETE FROM embeddings WHERE rowid <= (SELECT MAX(rowid) FROM embeddings)-?", (MAX_CACHE_ROWS,))
        return vector, known_skills

    def score(self, user, job, company_name):
        from backend.pipeline_skeleton import extract_job_description
        description = extract_job_description(job)
        resume = user["resume_text"]
        if type(user.get("id")) is not int or user["id"] <= 0:
            raise ValueError("Local scoring requires a positive user ID")
        if not isinstance(resume, str) or not resume.strip():
            raise ValueError("Local scoring requires a nonempty resume")
        key = (user["id"], similarity.text_hash(resume), similarity.text_hash(description), self.version)
        row = self.conn.execute("SELECT result_json FROM results WHERE user_id=? AND resume_hash=? AND jd_hash=? AND version=?", key).fetchone()
        if row:
            return similarity.validate_result(json.loads(row[0]), resume, description)
        resume_vector, resume_skills = self.embedding(resume, "resume:" + str(user["id"]))
        jd_vector, jd_skills = self.embedding(description, "jd")
        cosine = max(-1.0, min(1.0, sum(a * b for a, b in zip(resume_vector, jd_vector))))
        value = similarity.result_from_skills(cosine, resume_skills, jd_skills)
        with self.conn:
            self.conn.execute("INSERT INTO results VALUES(?,?,?,?,?,?)",
                              (*key, json.dumps(value, allow_nan=False), datetime.now(timezone.utc).isoformat()))
            self.conn.execute("DELETE FROM results WHERE rowid <= (SELECT MAX(rowid) FROM results)-?", (MAX_CACHE_ROWS,))
        return value
