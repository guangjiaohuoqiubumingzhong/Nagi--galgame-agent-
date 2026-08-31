"""Versioned terminology and character-name rule contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field


TERMINOLOGY_SCHEMA_VERSION = 1
TERMINOLOGY_SEVERITIES = frozenset({"error", "warning", "review"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


def _nonempty(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _canonical_sha256(payload):
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()


def _validate_severity(value):
    if value not in TERMINOLOGY_SEVERITIES:
        raise ValueError("unsupported terminology severity")


def _validate_identifier(value, field_name):
    _nonempty(value, field_name)
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a stable identifier")


@dataclass(frozen=True)
class TerminologyRule:
    """One source-term rule; `target=None` marks an unresolved term."""

    rule_id: str
    source: str
    target: str | None = None
    variants: tuple[str, ...] = ()
    severity: str = "error"
    note: str | None = field(default=None, repr=False)

    def __post_init__(self):
        _validate_identifier(self.rule_id, "terminology rule_id")
        _nonempty(self.source, "terminology source")
        if self.target is not None:
            _nonempty(self.target, "terminology target")
        if not isinstance(self.variants, tuple) or any(
            not isinstance(value, str) or not value.strip() for value in self.variants
        ):
            raise TypeError("terminology variants must be a tuple of non-empty strings")
        if self.target is not None and self.target in self.variants:
            raise ValueError("terminology target must not be repeated in variants")
        _validate_severity(self.severity)
        if self.note is not None and not isinstance(self.note, str):
            raise TypeError("terminology note must be a string or None")

    def identity_dict(self):
        return {
            "rule_id": self.rule_id,
            "source_sha256": _canonical_sha256(self.source),
            "target_sha256": _canonical_sha256(self.target) if self.target is not None else None,
            "variant_sha256": [_canonical_sha256(value) for value in self.variants],
            "severity": self.severity,
        }

    def to_dict(self, *, include_content=True):
        payload = {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "variant_count": len(self.variants),
        }
        if include_content:
            payload.update(
                {
                    "source": self.source,
                    "target": self.target,
                    "variants": list(self.variants),
                    "note": self.note,
                }
            )
        else:
            payload.update(self.identity_dict())
        return payload


@dataclass(frozen=True)
class CharacterNameRule:
    """Canonical and reviewable alternate names for one Segment speaker."""

    rule_id: str
    speaker: str
    canonical: str
    source_names: tuple[str, ...] = ()
    variants: tuple[str, ...] = ()
    severity: str = "review"

    def __post_init__(self):
        _validate_identifier(self.rule_id, "character rule_id")
        _nonempty(self.speaker, "character speaker")
        _nonempty(self.canonical, "character canonical")
        for field_name, values in (
            ("character source_names", self.source_names),
            ("character variants", self.variants),
        ):
            if not isinstance(values, tuple) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise TypeError(f"{field_name} must be a tuple of non-empty strings")
        if self.canonical in self.variants:
            raise ValueError("character canonical must not be repeated in variants")
        _validate_severity(self.severity)

    def identity_dict(self):
        return {
            "rule_id": self.rule_id,
            "speaker_sha256": _canonical_sha256(self.speaker),
            "canonical_sha256": _canonical_sha256(self.canonical),
            "source_name_sha256": [_canonical_sha256(value) for value in self.source_names],
            "variant_sha256": [_canonical_sha256(value) for value in self.variants],
            "severity": self.severity,
        }

    def to_dict(self, *, include_content=True):
        payload = {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "source_name_count": len(self.source_names),
            "variant_count": len(self.variants),
        }
        if include_content:
            payload.update(
                {
                    "speaker": self.speaker,
                    "canonical": self.canonical,
                    "source_names": list(self.source_names),
                    "variants": list(self.variants),
                }
            )
        else:
            payload.update(self.identity_dict())
        return payload


@dataclass(frozen=True)
class TerminologySnapshot:
    """Immutable rule set bound to the run's terminology_version."""

    version: str
    terms: tuple[TerminologyRule, ...] = ()
    characters: tuple[CharacterNameRule, ...] = ()

    def __post_init__(self):
        _nonempty(self.version, "terminology version")
        if not isinstance(self.terms, tuple) or any(
            not isinstance(item, TerminologyRule) for item in self.terms
        ):
            raise TypeError("terms must contain TerminologyRule values")
        if not isinstance(self.characters, tuple) or any(
            not isinstance(item, CharacterNameRule) for item in self.characters
        ):
            raise TypeError("characters must contain CharacterNameRule values")
        rule_ids = [item.rule_id for item in self.terms + self.characters]
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("terminology rule IDs must be globally unique")
        source_values = [item.source for item in self.terms]
        if len(set(source_values)) != len(source_values):
            raise ValueError("terminology source values must be unique")
        speakers = [item.speaker for item in self.characters]
        if len(set(speakers)) != len(speakers):
            raise ValueError("character speakers must be unique")

    @property
    def snapshot_sha256(self):
        return _canonical_sha256(
            {
                "schema_version": TERMINOLOGY_SCHEMA_VERSION,
                "version": self.version,
                "terms": [item.identity_dict() for item in self.terms],
                "characters": [item.identity_dict() for item in self.characters],
            }
        )

    def to_dict(self, *, include_content=False):
        return {
            "schema_version": TERMINOLOGY_SCHEMA_VERSION,
            "version": self.version,
            "snapshot_sha256": self.snapshot_sha256,
            "term_count": len(self.terms),
            "character_count": len(self.characters),
            "terms": [item.to_dict(include_content=include_content) for item in self.terms],
            "characters": [
                item.to_dict(include_content=include_content) for item in self.characters
            ],
        }

    def to_json(self, *, include_content=False):
        return json.dumps(self.to_dict(include_content=include_content), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


__all__ = [
    "TERMINOLOGY_SCHEMA_VERSION",
    "CharacterNameRule",
    "TerminologyRule",
    "TerminologySnapshot",
]
