"""Pinned, local-only neural retrieval models. No downloads during translation.

Provision explicitly with ``python -m nagi.rag.semantic download``. ONNX avoids
loading pickle weights or executing model-repository Python code.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from ..paths import state_root


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repo: str
    revision: str
    weights: str
    weights_sha256: str
    tokenizer: str
    tokenizer_sha256: str

    @property
    def identity(self):
        return f"{self.repo}@{self.revision}:{self.weights_sha256}:onnx-v1"


EMBEDDING = ModelSpec(
    "multilingual-e5-small",
    "intfloat/multilingual-e5-small",
    "614241f622f53c4eeff9890bdc4f31cfecc418b3",
    "onnx/model.onnx",
    "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
    "onnx/tokenizer.json",
    "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
)
RERANKER = ModelSpec(
    "mmarco-minilm",
    "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
    "1427fd652930e4ba29e8149678df786c240d8825",
    "onnx/model_quint8_avx2.onnx",
    "6c2513767fb63d008a4377bef7a7a3555433d9436342bb53e35a3a72ffc52d4b",
    "tokenizer.json",
    "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626",
)


def default_model_root():
    return state_root() / "models" / "translation-rag"


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _check_file(path, expected):
    if path.is_symlink() or not path.is_file() or file_digest(path) != expected:
        raise ValueError(f"local RAG model file is missing or corrupted: {path.name}")


class LocalOnnxModel:
    def __init__(self, spec, root):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.spec = spec
        self.identity = spec.identity
        self._np = np
        directory = Path(root) / spec.name
        weights = directory / "model.onnx"
        tokenizer = directory / "tokenizer.json"
        _check_file(weights, spec.weights_sha256)
        _check_file(tokenizer, spec.tokenizer_sha256)
        self.tokenizer = Tokenizer.from_file(str(tokenizer))
        self.tokenizer.no_truncation()
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(weights), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._inputs = {item.name for item in self.session.get_inputs()}
        self._lock = threading.RLock()

    def count_tokens(self, text):
        with self._lock:
            return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def _run(self, texts):
        # Shared tokenizers and sessions are protected against concurrent jobs.
        with self._lock:
            encoded = self.tokenizer.encode_batch(list(texts))
            if any(len(item.ids) > 512 for item in encoded):
                raise ValueError(
                    "RAG model input exceeds 512 tokens; refusing truncation"
                )
            values = {
                "input_ids": [item.ids for item in encoded],
                "attention_mask": [item.attention_mask for item in encoded],
                "token_type_ids": [item.type_ids for item in encoded],
            }
            inputs = {
                key: self._np.asarray(value, dtype=self._np.int64)
                for key, value in values.items()
                if key in self._inputs
            }
            output = self.session.run(None, inputs)[0]
            return output, inputs["attention_mask"]

    def embed(self, texts, *, query=False):
        if not texts:
            return []
        prefix = "query: " if query else "passage: "
        output, mask = self._run([prefix + text for text in texts])
        if output.ndim != 3 or output.shape[2] != 384:
            raise ValueError("unexpected E5 embedding output shape")
        vectors = (output * mask[..., None]).sum(axis=1) / mask.sum(axis=1)[:, None]
        norms = self._np.linalg.norm(vectors, axis=1, keepdims=True)
        if not self._np.isfinite(vectors).all() or (norms == 0).any():
            raise ValueError("invalid E5 embedding output")
        return (vectors / norms).tolist()

    def fits_pair(self, query, passage):
        with self._lock:
            return len(self.tokenizer.encode(query, passage).ids) <= 512

    def rerank(self, query, passages):
        if not passages:
            return []
        output, _ = self._run([(query, passage) for passage in passages])
        if output.shape != (len(passages), 1) or not self._np.isfinite(output).all():
            raise ValueError("unexpected reranker output shape")
        # Raw cross-encoder logits, NOT calibrated probabilities.
        return output[:, 0].tolist()


@lru_cache(maxsize=4)
def _load_local(spec, root):
    return LocalOnnxModel(spec, root)


def local_models(root=None):
    root = Path(root or default_model_root()).resolve()
    models, missing = [], []
    for spec in (EMBEDDING, RERANKER):
        if not all(
            (root / spec.name / name).is_file()
            for name in ("model.onnx", "tokenizer.json")
        ):
            models.append(None)
            missing.append(spec.name)
            continue
        try:
            models.append(_load_local(spec, str(root)))
        except ImportError:
            models.append(None)
            missing.append("install nagi[rag]")
        # Corrupt installed weights fail closed; never silently change a run.
    return (*models, tuple(missing))


def model_status(root=None):
    """Lightweight UI status; actual integrity checks happen at model load."""
    import importlib.util

    root = Path(root or default_model_root())
    dependencies = all(
        importlib.util.find_spec(name) is not None
        for name in ("numpy", "onnxruntime", "tokenizers")
    )
    present = [
        dependencies
        and all(
            (root / spec.name / name).is_file()
            for name in ("model.onnx", "tokenizer.json")
        )
        for spec in (EMBEDDING, RERANKER)
    ]
    return {
        "mode": "hybrid" if present[0] else "bm25",
        "embedding": EMBEDDING.repo if present[0] else None,
        "reranker": RERANKER.repo if present[1] else None,
    }


class RetrievalCache:
    """Content-addressed vectors/scores, containing neither credentials nor prose."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.memory = {}
        self.hits = 0
        self.misses = 0
        if self.path:
            if self.path.is_symlink():
                raise ValueError("RAG cache must not be a symlink")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path, timeout=30) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS retrieval (key TEXT PRIMARY KEY, value TEXT NOT NULL, digest TEXT NOT NULL)"
                )

    @staticmethod
    def key(model, kind, texts):
        raw = json.dumps(["rag-cache-v2", model, kind, texts], ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key):
        if key in self.memory:
            self.hits += 1
            return self.memory[key]
        if self.path:
            with sqlite3.connect(self.path, timeout=30) as db:
                row = db.execute(
                    "SELECT value, digest FROM retrieval WHERE key=?", (key,)
                ).fetchone()
            if row:
                if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
                    raise ValueError("RAG cache checksum mismatch")
                value = json.loads(row[0])
                self.memory[key] = value
                self.hits += 1
                return value
        self.misses += 1
        return None

    def put(self, key, value):
        raw = json.dumps(value, allow_nan=False, separators=(",", ":"))
        if self.path:
            with sqlite3.connect(self.path, timeout=30) as db:
                db.execute(
                    "INSERT OR REPLACE INTO retrieval VALUES (?, ?, ?)",
                    (key, raw, hashlib.sha256(raw.encode()).hexdigest()),
                )
        self.memory[key] = value

    def embeddings(self, model, texts, *, query=False, policy="scene-chunks-v2"):
        kind = ("query:" if query else "passage:") + policy
        keys = [self.key(model.identity, kind, text) for text in texts]
        values = [self.get(key) for key in keys]
        missing = [i for i, value in enumerate(values) if value is None]
        for start in range(0, len(missing), 8):
            indexes = missing[start : start + 8]
            encoded = model.embed([texts[i] for i in indexes], query=query)
            if len(encoded) != len(indexes):
                raise ValueError("embedding result count mismatch")
            for i, vector in zip(indexes, encoded):
                _validate_vector(vector)
                self.put(keys[i], vector)
                values[i] = vector
        for value in values:
            _validate_vector(value)
        if values and any(len(value) != len(values[0]) for value in values):
            raise ValueError("embedding dimensions changed")
        return values

    def scores(self, model, query, passages):
        keys = [
            self.key(model.identity, "rerank-v2", [query, text]) for text in passages
        ]
        values = [self.get(key) for key in keys]
        missing = [i for i, value in enumerate(values) if value is None]
        if missing:
            scores = model.rerank(query, [passages[i] for i in missing])
            if len(scores) != len(missing):
                raise ValueError("rerank result count mismatch")
            for i, score in zip(missing, scores):
                if type(score) not in (float, int) or not math.isfinite(score):
                    raise ValueError("invalid rerank score")
                self.put(keys[i], float(score))
                values[i] = float(score)
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError("invalid cached rerank score")
        return values


def _validate_vector(value):
    if (
        not isinstance(value, (list, tuple))
        or not value
        or len(value) > 16384
        or any(type(v) not in (float, int) or not math.isfinite(v) for v in value)
        or not 0.99 <= sum(v * v for v in value) <= 1.01
    ):
        raise ValueError("invalid normalized embedding vector")


def download_models(root=None):
    """Explicit provisioning only; download pinned public data, never game text."""
    import tempfile
    import urllib.request

    root = Path(root or default_model_root()).resolve()
    for spec in (EMBEDDING, RERANKER):
        directory = root / spec.name
        directory.mkdir(parents=True, exist_ok=True)
        for remote, name, digest in (
            (spec.weights, "model.onnx", spec.weights_sha256),
            (spec.tokenizer, "tokenizer.json", spec.tokenizer_sha256),
        ):
            target = directory / name
            if target.exists():
                _check_file(target, digest)
                print(f"Verified {spec.name}/{name}", flush=True)
                continue
            url = f"https://huggingface.co/{spec.repo}/resolve/{spec.revision}/{remote}"
            print(f"Downloading {spec.name}/{name}", flush=True)
            staging = None
            try:
                with (
                    urllib.request.urlopen(url, timeout=30) as response,
                    tempfile.NamedTemporaryFile(
                        dir=directory, suffix=".download", delete=False
                    ) as stream,
                ):
                    staging = Path(stream.name)
                    while block := response.read(1024 * 1024):
                        stream.write(block)
                _check_file(staging, digest)
                staging.replace(target)
            finally:
                if staging is not None and staging.exists():
                    staging.unlink()  # Only our own incomplete download.
            print(f"Verified {spec.name}/{name}", flush=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "status"))
    parser.add_argument("--model-dir", type=Path)
    args = parser.parse_args()
    if args.action == "download":
        download_models(args.model_dir)
    else:
        embedder, reranker, missing = local_models(args.model_dir)
        print(
            json.dumps(
                {
                    "embedding": embedder.identity if embedder else None,
                    "reranker": reranker.identity if reranker else None,
                    "missing": missing,
                },
                ensure_ascii=False,
            )
        )
