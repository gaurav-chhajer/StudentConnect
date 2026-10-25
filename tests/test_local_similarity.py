"""Deterministic local ranking tests without downloads, external services, or torch."""

import json
import math
import re
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend import local_embeddings, similarity


class Tokenizer:
    def __call__(self, text, **kwargs):
        return {"offset_mapping": [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]}

    def encode(self, text, **kwargs):
        return list(range(len(text.split()) + (2 if kwargs.get("add_special_tokens") else 0)))


class Vector(list):
    def tolist(self):
        return list(self)


class Model:
    tokenizer = Tokenizer()
    max_seq_length = 256

    def __init__(self):
        self.calls = []

    def encode(self, sections, **kwargs):
        self.calls.extend(sections)
        return [Vector([float(bool(re.search(r"\bpython\b", text, re.I))),
                        float(bool(re.search(r"\bsql\b", text, re.I))), 1.0] + [0.0] * 381) for text in sections]


class SimilarityTests(unittest.TestCase):
    def test_skill_boundaries_aliases_and_unknowns(self):
        self.assertEqual(similarity.skills("JavaScript React.js C++ .NET GCP"), [".NET", "C++", "Google Cloud", "JavaScript", "React"])
        matched, absent, overlap = similarity.skill_comparison("Python Python", "Python SQL SQL")
        self.assertEqual((matched, absent, overlap), (["Python"], ["SQL"], 0.5))
        self.assertEqual(similarity.skill_comparison("Python", "Team member"), ([], [], 0.0))
        self.assertEqual(similarity.result(0.5, "Python", "Team member")["rank_score"], 50)

    def test_invalid_similarity_evidence_and_versions_rejected(self):
        for cosine in (None, True, "0.1", math.nan, math.inf, -1.01, 1.01):
            with self.subTest(cosine=cosine), self.assertRaises(ValueError):
                similarity.result(cosine, "Python", "Python SQL")
        value = similarity.result(0.5, "Python", "Python SQL")
        self.assertEqual(value["rank_score"], 50)
        for changes in ({"rank_score": 100}, {"matched_skills": ["SQL"]}, {"skill_overlap": True},
                        {"model_revision": "other"}, {"scoring_version": "other"}, {"extra": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                similarity.validate_result(dict(value, **changes), "Python", "Python SQL")

    def test_long_documents_keep_tail_and_bound_every_chunk(self):
        text = "intro " * 900 + "Python SQL tail"
        sections = list(local_embeddings.chunks(text, Tokenizer(), 256))
        self.assertGreater(len(sections), 1)
        self.assertTrue(any("Python SQL tail" in section for section in sections))
        self.assertTrue(all(len(section.split()) <= 224 for section in sections))
        for invalid in ("", "   ", "x" * (local_embeddings.MAX_DOCUMENT_CHARACTERS + 1)):
            with self.assertRaises(ValueError):
                list(local_embeddings.chunks(invalid, Tokenizer(), 256))

    def test_cache_reuses_documents_and_isolates_users_and_versions(self):
        model = Model()
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, \
                closing(local_embeddings.LocalScorer(model, Path(directory) / "cache.db")) as scorer:
            job = dict(raw_json=json.dumps({"content": "Python SQL"}))
            user = dict(id=1, resume_text="Python project")
            value = scorer.score(user, job, "Synthetic")
            self.assertEqual(len(model.calls), 2)
            self.assertEqual(scorer.score(user, job, "Synthetic"), value)
            self.assertEqual(len(model.calls), 2)
            scorer.score(dict(user, id=2), job, "Synthetic")
            self.assertEqual(len(model.calls), 3)  # Shared public JD; private resume embedding.
            scorer.score(dict(user, resume_text="SQL project"), job, "Synthetic")
            self.assertEqual(len(model.calls), 4)
            scorer.version += "-new"
            scorer.score(user, job, "Synthetic")
            self.assertEqual(len(model.calls), 6)
            cached = " ".join(str(row) for row in scorer.conn.execute("SELECT * FROM embeddings"))
            self.assertNotIn("Python project", cached)
            self.assertNotIn("SQL project", cached)

    def test_cache_size_is_bounded_and_invalid_vectors_fail(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, \
                closing(local_embeddings.LocalScorer(Model(), Path(directory) / "cache.db")) as scorer:
            with patch.object(local_embeddings, "MAX_CACHE_ROWS", 2):
                for number in range(4):
                    scorer.embedding("Python " + str(number), "jd")
            self.assertEqual(scorer.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 2)
        for vector in ([], [0] * 384, [math.nan] * 384, [True] * 384):
            with self.assertRaises(ValueError):
                local_embeddings.normalize(vector)

    def test_missing_model_fails_without_importing_or_downloading(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, \
                patch.object(local_embeddings, "MODEL_DIR", Path(directory)):
            with self.assertRaisesRegex(RuntimeError, "prepare_similarity"):
                local_embeddings.load_model()
