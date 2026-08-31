"""Planning, transactional persistence, and loading for keyword indexes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .ingest import IngestedKeywordCorpus, ingest_segment_corpus
from .models import (
    KEYWORD_INDEX_VERSION,
    RAG_SCHEMA_VERSION,
    TOKENIZER_VERSION,
    KeywordDocument,
)


INDEX_SCOPES = frozenset({"translatable", "all"})
MAX_INDEX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
MAX_INDEX_MANIFEST_BYTES = 4 * 1024 * 1024
COMMIT_RENAME_ATTEMPTS = 6
COMMIT_RENAME_DELAY_SECONDS = 0.05


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path, parent):
    path_text = os.path.normcase(str(Path(path).resolve()))
    parent_text = os.path.normcase(str(Path(parent).resolve()))
    try:
        return os.path.commonpath([path_text, parent_text]) == parent_text
    except ValueError:
        return False


def _git_root_for(path):
    candidate = Path(path).resolve()
    if not candidate.is_dir():
        candidate = candidate.parent
    for current in (candidate, *candidate.parents):
        if (current / ".git").exists():
            return current
    return None


def _commit_staging_directory(staging, output):
    for attempt in range(COMMIT_RENAME_ATTEMPTS):
        try:
            os.rename(staging, output)
            return
        except PermissionError:
            if attempt + 1 == COMMIT_RENAME_ATTEMPTS:
                raise
            time.sleep(COMMIT_RENAME_DELAY_SECONDS * (2**attempt))


def _index_id(corpus, scope):
    identity = {
        "index_version": KEYWORD_INDEX_VERSION,
        "scope": scope,
        "source_sha256": corpus.source_sha256,
        "tokenizer_version": TOKENIZER_VERSION,
    }
    canonical = json.dumps(identity, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "kw_v1_" + hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class KeywordIndexPlan:
    schema_version: int
    index_version: int
    index_id: str | None
    status: str
    reason: str
    corpus_path: str
    output_dir: str
    scope: str
    corpus: IngestedKeywordCorpus | None = field(default=None, repr=False)
    warnings: tuple[str, ...] = ()

    def to_dict(self):
        corpus = self.corpus
        return {
            "schema_version": self.schema_version,
            "index_version": self.index_version,
            "index_id": self.index_id,
            "status": self.status,
            "reason": self.reason,
            "corpus_path": self.corpus_path,
            "output_dir": self.output_dir,
            "scope": self.scope,
            "tokenizer": {
                "version": TOKENIZER_VERSION,
                "normalization": "unicode-nfkc-casefold",
                "tokens": ["unicode-word", "cjk-unigram", "cjk-bigram"],
            },
            "source": {
                "segments_path": corpus.segments_path if corpus else None,
                "segments_sha256": corpus.source_sha256 if corpus else None,
                "segments_size_bytes": corpus.source_size_bytes if corpus else None,
            },
            "summary": {
                "source_segment_count": corpus.segment_count if corpus else 0,
                "source_translatable_count": (
                    corpus.translatable_segment_count if corpus else 0
                ),
                "indexed_document_count": len(corpus.documents) if corpus else 0,
                "token_count": len(corpus.postings) if corpus else 0,
                "posting_count": corpus.posting_count if corpus else 0,
                "kind_counts": dict(corpus.kind_counts) if corpus else {},
            },
            "warnings": list(self.warnings),
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(frozen=True)
class KeywordIndexResult:
    schema_version: int
    index_version: int
    index_id: str | None
    status: str
    reason: str
    output_dir: str
    manifest_path: str | None
    documents_path: str | None
    postings_path: str | None
    document_count: int
    token_count: int
    posting_count: int
    warnings: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "index_version": self.index_version,
            "index_id": self.index_id,
            "status": self.status,
            "reason": self.reason,
            "output_dir": self.output_dir,
            "artifacts": {
                "manifest_path": self.manifest_path,
                "documents_path": self.documents_path,
                "postings_path": self.postings_path,
            },
            "summary": {
                "document_count": self.document_count,
                "token_count": self.token_count,
                "posting_count": self.posting_count,
            },
            "warnings": list(self.warnings),
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(frozen=True)
class LoadedKeywordIndex:
    index_id: str
    scope: str
    source_segments_path: str
    source_segments_sha256: str
    documents: tuple[KeywordDocument, ...]
    postings: dict[str, tuple[tuple[int, int], ...]]
    average_document_length: float
    manifest: dict = field(repr=False)


def _validate_output_location(corpus_path, output):
    corpus = Path(corpus_path).expanduser().resolve()
    source_root = corpus if corpus.is_dir() else corpus.parent
    if output.parent == output:
        raise ValueError("filesystem root cannot be used as an index output directory")
    if _is_within(output, source_root) or _is_within(source_root, output):
        raise ValueError("index output directory must not overlap the source corpus")
    git_root = _git_root_for(output)
    if git_root is not None:
        raise ValueError(f"index output directory must be outside Git worktree: {git_root}")


def build_keyword_index_plan(corpus_path, output_dir, *, scope="translatable"):
    corpus_path = str(Path(corpus_path).expanduser().resolve())
    output = Path(output_dir).expanduser().resolve()
    normalized_scope = str(scope).strip().casefold()
    try:
        if normalized_scope not in INDEX_SCOPES:
            raise ValueError("index scope must be translatable or all")
        if output.exists():
            raise ValueError("index output directory already exists; overwrite is disabled")
        _validate_output_location(corpus_path, output)
        corpus = ingest_segment_corpus(
            corpus_path,
            include_nontranslatable=normalized_scope == "all",
        )
    except (OSError, ValueError) as exc:
        return KeywordIndexPlan(
            schema_version=RAG_SCHEMA_VERSION,
            index_version=KEYWORD_INDEX_VERSION,
            index_id=None,
            status="invalid_input",
            reason=str(exc),
            corpus_path=corpus_path,
            output_dir=str(output),
            scope=normalized_scope,
        )
    return KeywordIndexPlan(
        schema_version=RAG_SCHEMA_VERSION,
        index_version=KEYWORD_INDEX_VERSION,
        index_id=_index_id(corpus, normalized_scope),
        status="ready",
        reason="Segment v1 corpus and keyword postings are ready for transactional publish",
        corpus_path=corpus_path,
        output_dir=str(output),
        scope=normalized_scope,
        corpus=corpus,
    )


def _write_jsonl(path, values):
    digest = hashlib.sha256()
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        for value in values:
            line = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n"
            encoded = line.encode("utf-8")
            digest.update(encoded)
            stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    if _sha256_file(path) != digest.hexdigest():
        raise OSError(f"{path.name} hash changed during staging verification")
    return digest.hexdigest()


def _result(plan, status, reason, *, published=False):
    corpus = plan.corpus
    output = Path(plan.output_dir)
    return KeywordIndexResult(
        schema_version=RAG_SCHEMA_VERSION,
        index_version=KEYWORD_INDEX_VERSION,
        index_id=plan.index_id,
        status=status,
        reason=reason,
        output_dir=plan.output_dir,
        manifest_path=str(output / "manifest.json") if published else None,
        documents_path=str(output / "documents.jsonl") if published else None,
        postings_path=str(output / "postings.jsonl") if published else None,
        document_count=len(corpus.documents) if corpus else 0,
        token_count=len(corpus.postings) if corpus else 0,
        posting_count=corpus.posting_count if corpus else 0,
        warnings=plan.warnings,
    )


def apply_keyword_index_plan(plan):
    if plan.status != "ready" or plan.corpus is None:
        return _result(plan, "blocked", f"keyword index plan is not ready: {plan.reason}")
    output = Path(plan.output_dir)
    if output.exists():
        return _result(plan, "output_exists", "index output already exists; overwrite is disabled")
    if _sha256_file(plan.corpus.segments_path) != plan.corpus.source_sha256:
        return _result(plan, "source_changed", "segments.jsonl changed after index planning")
    staging = None
    try:
        _validate_output_location(plan.corpus_path, output)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            return _result(
                plan,
                "output_exists",
                "index output appeared during preflight; overwrite is disabled",
            )
        staging = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=str(output.parent))
        )
        documents_hash = _write_jsonl(
            staging / "documents.jsonl",
            (document.to_dict() for document in plan.corpus.documents),
        )
        postings_hash = _write_jsonl(
            staging / "postings.jsonl",
            (
                {
                    "token": token,
                    "document_frequency": len(values),
                    "postings": [list(value) for value in values],
                }
                for token, values in plan.corpus.postings
            ),
        )
        average_length = (
            sum(document.token_count for document in plan.corpus.documents)
            / len(plan.corpus.documents)
            if plan.corpus.documents
            else 0.0
        )
        manifest = {
            "schema_version": RAG_SCHEMA_VERSION,
            "index_version": KEYWORD_INDEX_VERSION,
            "index_id": plan.index_id,
            "scope": plan.scope,
            "source": {
                "segments_path": plan.corpus.segments_path,
                "segments_sha256": plan.corpus.source_sha256,
                "segments_size_bytes": plan.corpus.source_size_bytes,
                "segment_count": plan.corpus.segment_count,
            },
            "tokenizer": {
                "version": TOKENIZER_VERSION,
                "normalization": "unicode-nfkc-casefold",
                "tokens": ["unicode-word", "cjk-unigram", "cjk-bigram"],
            },
            "summary": {
                "document_count": len(plan.corpus.documents),
                "token_count": len(plan.corpus.postings),
                "posting_count": plan.corpus.posting_count,
                "average_document_length": average_length,
            },
            "artifacts": {
                "documents": {
                    "path": "documents.jsonl",
                    "sha256": documents_hash,
                },
                "postings": {
                    "path": "postings.jsonl",
                    "sha256": postings_hash,
                },
            },
        }
        with (staging / "manifest.json").open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _commit_staging_directory(staging, output)
        staging = None
    except (OSError, TypeError, ValueError) as exc:
        return _result(
            plan,
            "write_failed",
            f"transactional keyword index publish failed: {exc}",
        )
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return _result(
        plan,
        "published",
        "keyword index published transactionally to a new directory",
        published=True,
    )


def _safe_artifact(root, relative_name):
    if not isinstance(relative_name, str) or not relative_name or "/" in relative_name or "\\" in relative_name:
        raise ValueError("index artifact path must be a direct relative filename")
    path = root / relative_name
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_file() or not _is_within(resolved, root):
        raise ValueError("index artifact is not a regular in-index file")
    if resolved.stat().st_size > MAX_INDEX_ARTIFACT_BYTES:
        raise ValueError("index artifact exceeds the loading size limit")
    return resolved


def load_keyword_index(index_dir, *, source_corpus=None, verify_source=True):
    root = Path(index_dir).expanduser().resolve()
    manifest_path = root / "manifest.json"
    if not root.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("keyword index directory or manifest.json is unavailable")
    if manifest_path.stat().st_size > MAX_INDEX_MANIFEST_BYTES:
        raise ValueError("keyword index manifest exceeds the loading size limit")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if manifest.get("schema_version") != RAG_SCHEMA_VERSION:
            raise ValueError("unsupported RAG schema version")
        if manifest.get("index_version") != KEYWORD_INDEX_VERSION:
            raise ValueError("unsupported keyword index version")
        artifacts = manifest["artifacts"]
        documents_path = _safe_artifact(root, artifacts["documents"]["path"])
        postings_path = _safe_artifact(root, artifacts["postings"]["path"])
        if _sha256_file(documents_path) != artifacts["documents"]["sha256"]:
            raise ValueError("documents.jsonl hash does not match index manifest")
        if _sha256_file(postings_path) != artifacts["postings"]["sha256"]:
            raise ValueError("postings.jsonl hash does not match index manifest")
        documents = tuple(
            KeywordDocument.from_dict(json.loads(line))
            for line in documents_path.read_text(encoding="utf-8").splitlines()
        )
        if [document.doc_id for document in documents] != list(range(len(documents))):
            raise ValueError("keyword document IDs are not contiguous")
        postings = {}
        for line in postings_path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            token = item["token"]
            values = tuple((int(doc_id), int(tf)) for doc_id, tf in item["postings"])
            if token in postings or item["document_frequency"] != len(values):
                raise ValueError("keyword postings contain duplicate or invalid tokens")
            if any(doc_id < 0 or doc_id >= len(documents) or tf <= 0 for doc_id, tf in values):
                raise ValueError("keyword posting references an invalid document")
            postings[token] = values
        summary = manifest["summary"]
        if summary["document_count"] != len(documents) or summary["token_count"] != len(postings):
            raise ValueError("index summary does not match loaded artifacts")
        if verify_source:
            source_path = (
                Path(source_corpus).expanduser().resolve()
                if source_corpus is not None
                else Path(manifest["source"]["segments_path"])
            )
            if source_path.is_dir():
                source_path = source_path / "segments.jsonl"
            if source_path.is_symlink() or not source_path.is_file():
                raise ValueError("source segments.jsonl is unavailable for freshness validation")
            if _sha256_file(source_path) != manifest["source"]["segments_sha256"]:
                raise ValueError("keyword index is stale because source corpus hash changed")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"keyword index validation failed: {exc}")
    return LoadedKeywordIndex(
        index_id=manifest["index_id"],
        scope=manifest["scope"],
        source_segments_path=manifest["source"]["segments_path"],
        source_segments_sha256=manifest["source"]["segments_sha256"],
        documents=documents,
        postings=postings,
        average_document_length=float(summary["average_document_length"]),
        manifest=manifest,
    )


def render_index_plan_text(plan):
    summary = plan.to_dict()["summary"]
    return "\n".join(
        [
            f"Keyword index plan: {plan.status}",
            f"corpus: {plan.corpus_path}",
            f"output_dir: {plan.output_dir}",
            f"scope: {plan.scope}",
            (
                f"segments: {summary['source_segment_count']}; "
                f"documents: {summary['indexed_document_count']}; "
                f"tokens: {summary['token_count']}; postings: {summary['posting_count']}"
            ),
            f"index_id: {plan.index_id or 'not available'}",
            f"reason: {plan.reason}",
        ]
    )


def render_index_result_text(result):
    return "\n".join(
        [
            f"Keyword index publish: {result.status}",
            f"output_dir: {result.output_dir}",
            (
                f"documents: {result.document_count}; tokens: {result.token_count}; "
                f"postings: {result.posting_count}"
            ),
            f"manifest: {result.manifest_path or 'not written'}",
            f"reason: {result.reason}",
        ]
    )
