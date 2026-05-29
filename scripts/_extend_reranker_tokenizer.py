#!/usr/bin/env python
"""ADR-0011 Phase 1 (part 2) — extend the bge-reranker tokenizer with domain vocab.

Reads the TSV emitted by ``_extract_domain_vocab.py`` and:

  1. Loads the stock ``BAAI/bge-reranker-base`` tokenizer + model.
  2. Calls ``tokenizer.add_tokens(new_terms)`` to register the new
     whole-word tokens.
  3. Calls ``model.resize_token_embeddings(len(tokenizer))`` to grow
     the embedding matrix.
  4. For each newly-added token, initializes its embedding row as the
     mean of the embeddings of the subwords the term used to be
     tokenized to (standard recipe — preserves "the model already
     kinda knew this word from its parts").
  5. Saves the expanded tokenizer + model to the output dir. This dir
     becomes the input backbone for Phase 2 MLM continued pretraining.

Why mean-of-subword-embeddings:
* Random init wastes the warm-start the model already has.
* Copy-from-subword-zero only carries one piece of context.
* Mean is the proven recipe from the BioBERT / SciBERT literature.

Usage
-----

    docker exec georag-fastapi bash -c \\
        "python /app/scripts/_extend_reranker_tokenizer.py \\
            --vocab-tsv /tmp/vocab_candidates.tsv \\
            --output /tmp/hf_cache/_bge_extended/v1-2026-05-28 \\
            --base-model BAAI/bge-reranker-base"

The output dir contains the standard HF model artifacts
(config.json, model.safetensors, tokenizer.json, special_tokens_map.json,
tokenizer_config.json) plus a ``vocab_extension_manifest.json`` that
records which terms were added + the git_sha + the embedding-init recipe.

Idempotent: re-runs with the same input TSV produce the same output.
Terms already present in the tokenizer's vocab are silently skipped by
``add_tokens``.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("extend_reranker_tokenizer")


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL,
        ).decode().strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def _load_vocab_tsv(path: Path) -> list[str]:
    """Read the TSV emitted by _extract_domain_vocab.py — return ordered terms."""
    terms: list[str] = []
    with open(path) as fh:
        header = fh.readline().rstrip("\n").split("\t")
        if header[0] != "term":
            raise ValueError(f"unexpected header: {header}")
        for line in fh:
            cols = line.rstrip("\n").split("\t")
            if not cols or not cols[0]:
                continue
            terms.append(cols[0])
    logger.info("loaded %d candidate terms from %s", len(terms), path)
    return terms


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--vocab-tsv", required=True)
    p.add_argument("--base-model", default="BAAI/bge-reranker-base")
    p.add_argument("--output", required=True)
    args = p.parse_args()

    import torch  # noqa: PLC0415
    from transformers import AutoModelForSequenceClassification, AutoTokenizer  # noqa: PLC0415

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("loading base model + tokenizer: %s", args.base_model)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.base_model, num_labels=1,
    )

    pre_vocab_size = len(tokenizer)
    pre_emb_shape = tuple(model.get_input_embeddings().weight.shape)
    logger.info("pre-extension: vocab=%d, embedding shape=%s", pre_vocab_size, pre_emb_shape)

    # Phase 1: register the new terms with the tokenizer.
    terms = _load_vocab_tsv(Path(args.vocab_tsv))
    # add_tokens silently skips anything already present, returns the
    # count of newly-added tokens.
    n_added = tokenizer.add_tokens(terms)
    logger.info("tokenizer.add_tokens: added %d / %d (skipped %d already-present)",
                n_added, len(terms), len(terms) - n_added)

    post_vocab_size = len(tokenizer)
    logger.info("post-extension: vocab=%d (delta=%d)",
                post_vocab_size, post_vocab_size - pre_vocab_size)

    # Phase 2: resize the model embedding matrix.
    model.resize_token_embeddings(post_vocab_size)
    post_emb_shape = tuple(model.get_input_embeddings().weight.shape)
    logger.info("model.resize_token_embeddings: %s -> %s", pre_emb_shape, post_emb_shape)

    # Phase 3: initialize new token embeddings as the mean of the subword
    # token embeddings that previously represented the term.
    if n_added > 0:
        input_embeddings = model.get_input_embeddings().weight
        # Re-create a tokenizer snapshot from BEFORE the extension so
        # we can look up what subwords each new term used to map to.
        pre_tokenizer = AutoTokenizer.from_pretrained(args.base_model)

        added_count = 0
        for new_token_id in range(pre_vocab_size, post_vocab_size):
            term = tokenizer.convert_ids_to_tokens(new_token_id)
            # SentencePiece prefixes whole-words with the marker ▁;
            # tokenize the raw form against the PRE-extension tokenizer
            # to find the subword IDs we're meaning over.
            subword_ids = pre_tokenizer.encode(term, add_special_tokens=False)
            if not subword_ids:
                # Fallback: just use the [UNK] embedding rather than
                # leave it as the (random) resize-init.
                subword_ids = [pre_tokenizer.unk_token_id]
            sub_embs = input_embeddings[subword_ids].detach()
            mean_emb = sub_embs.mean(dim=0)
            with torch.no_grad():
                input_embeddings[new_token_id] = mean_emb
            added_count += 1

        logger.info("initialized %d new token embeddings via mean-of-subword recipe",
                    added_count)

    # Phase 4: persist tokenizer + model + manifest.
    logger.info("saving expanded tokenizer + model to %s", out_dir)
    tokenizer.save_pretrained(str(out_dir))
    model.save_pretrained(str(out_dir))

    manifest = {
        "adr":                  "ADR-0011 Phase 1",
        "base_model":           args.base_model,
        "vocab_tsv":            str(args.vocab_tsv),
        "pre_vocab_size":       pre_vocab_size,
        "post_vocab_size":      post_vocab_size,
        "tokens_added":         n_added,
        "tokens_requested":     len(terms),
        "pre_embedding_shape":  pre_emb_shape,
        "post_embedding_shape": post_emb_shape,
        "init_recipe":          "mean of subword embeddings (BioBERT/SciBERT recipe)",
        "git_sha":              _git_sha(),
        "extended_at_utc":      datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = out_dir / "vocab_extension_manifest.json"
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)
    logger.info("wrote: %s", manifest_path)

    logger.info(
        "Phase 1 complete. Next: docker exec georag-fastapi python "
        "/app/scripts/_train_mlm_continued.py --backbone %s --epochs 2",
        out_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
