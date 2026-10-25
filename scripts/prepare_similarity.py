"""Explicitly download only the pinned public CPU embedding model into data/."""

import argparse
import logging
import os

from backend.local_embeddings import MODEL_DIR, ROOT
from backend.similarity import MODEL_ID, MODEL_REVISION

logger = logging.getLogger(__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Download the pinned public model; no resume data is sent")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if not args.download:
        logger.info("Preview only: --download prepares the local embedding model; no files downloaded.")
        return 0
    os.environ.setdefault("HF_HOME", str(ROOT / "data/huggingface"))
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id=MODEL_ID, revision=MODEL_REVISION, local_dir=str(MODEL_DIR), token=False,
                          max_workers=1, allow_patterns=["config.json", "config_sentence_transformers.json",
                          "modules.json", "sentence_bert_config.json", "special_tokens_map.json", "tokenizer.json",
                          "tokenizer_config.json", "vocab.txt", "model.safetensors", "1_Pooling/config.json"])
    except Exception as error:
        # Final CLI boundary: report failure without exposing credentials in HTTP errors.
        logger.error("Local model preparation failed (%s); inspect dependency/network setup and retry explicitly.", type(error).__name__)
        return 1
    logger.info("Pinned local embedding model is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
