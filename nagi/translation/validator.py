"""Pure structural validation for translation candidates.

The validator deliberately keeps source and translated prose out of its
reports.  It is a safety gate for later patch-preview code, not a patch
writer and not a translation-quality grader.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace

from ..gameio.segments import SegmentInlineToken
from .models import TranslationCandidate, TranslationUnit
from .terminology import CharacterNameRule, TerminologySnapshot, TerminologyRule


TRANSLATION_VALIDATOR_VERSION = 1
VALIDATION_SEVERITIES = frozenset({"error", "warning", "review"})
VALIDATION_STATUSES = frozenset({"valid", "needs_review", "invalid"})
ACCEPTANCE_STATUSES = frozenset({"accepted", "review", "rejected"})

_PLACEHOLDER_RE = re.compile(r"\{[^{}\r\n]+\}|%[A-Za-z_][A-Za-z0-9_.]*%")
_BRACKET_TAG_RE = re.compile(r"\[[^\[\]\r\n]+\]")
_ESCAPE_RE = re.compile(r"\\[nrt\\]")
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _counter_delta(expected, actual):
    return {
        raw: actual.get(raw, 0) - expected.get(raw, 0)
        for raw in set(expected) | set(actual)
        if actual.get(raw, 0) != expected.get(raw, 0)
    }


def _newline_count(value: str) -> int:
    return sum(1 for _ in _NEWLINE_RE.finditer(value))


def _newline_style(value: str) -> str:
    styles = {match.group(0) for match in _NEWLINE_RE.finditer(value)}
    if not styles:
        return "none"
    if styles == {"\n"}:
        return "lf"
    if styles == {"\r\n"}:
        return "crlf"
    if styles == {"\r"}:
        return "cr"
    return "mixed"


def _scan_tokens(text: str, stream: str, expected: tuple[SegmentInlineToken, ...]):
    """Return `(kind, raw, start)` tokens in translated-text order."""

    if stream == "placeholder":
        return [
            ("placeholder", match.group(0), match.start())
            for match in _PLACEHOLDER_RE.finditer(text)
        ]
    if stream == "tag":
        matches = []
        for match in _BRACKET_TAG_RE.finditer(text):
            matches.append(("bracket_tag", match.group(0), match.start()))
        for match in _ESCAPE_RE.finditer(text):
            matches.append(("escape", match.group(0), match.start()))
        return sorted(matches, key=lambda item: (item[2], item[0], item[1]))

    # Future Segment token kinds still get exact-occurrence checking.  Their
    # lexical grammar is intentionally not guessed by the validator.
    raws = sorted({token.raw for token in expected}, key=lambda value: (-len(value), value))
    if not raws:
        return []
    pattern = re.compile("|".join(re.escape(raw) for raw in raws))
    return [(expected[0].kind, match.group(0), match.start()) for match in pattern.finditer(text)]


@dataclass(frozen=True)
class TranslationValidationIssue:
    """One content-free structural validation finding."""

    code: str
    severity: str
    unit_id: str
    segment_id: str
    rule_id: str | None = None
    speaker_sha256: str | None = None
    token_kind: str | None = None
    source_token_index: int | None = None
    actual_token_index: int | None = None
    expected_count: int | None = None
    actual_count: int | None = None
    expected_sha256: str | None = None
    actual_sha256: str | None = None
    line_number: int | None = None
    max_line_chars: int | None = None

    def __post_init__(self):
        if not self.code or not re.fullmatch(r"[a-z0-9_]+", self.code):
            raise ValueError("issue code must be a lowercase identifier")
        if self.severity not in VALIDATION_SEVERITIES:
            raise ValueError("unsupported validation issue severity")
        if not self.unit_id or not self.segment_id:
            raise ValueError("validation issue identities must be non-empty")
        for value, field_name in (
            (self.expected_sha256, "expected_sha256"),
            (self.actual_sha256, "actual_sha256"),
        ):
            if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")

    def to_dict(self):
        return {
            "code": self.code,
            "severity": self.severity,
            "unit_id": self.unit_id,
            "segment_id": self.segment_id,
            "rule_id": self.rule_id,
            "speaker_sha256": self.speaker_sha256,
            "token_kind": self.token_kind,
            "source_token_index": self.source_token_index,
            "actual_token_index": self.actual_token_index,
            "expected_count": self.expected_count,
            "actual_count": self.actual_count,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "line_number": self.line_number,
            "max_line_chars": self.max_line_chars,
        }


@dataclass(frozen=True)
class TranslationValidationReport:
    """Stable, content-free validation output for one candidate."""

    unit_id: str
    segment_id: str
    candidate_id: str
    source_text_sha256: str
    translated_text_sha256: str
    status: str
    patch_eligible: bool
    expected_token_count: int
    actual_token_count: int
    preserved_token_count: int
    newline_count_source: int
    newline_count_translated: int
    issue_counts: dict[str, int] = field(default_factory=dict)
    issues: tuple[TranslationValidationIssue, ...] = ()
    terminology_version: str | None = None

    def __post_init__(self):
        if self.status not in VALIDATION_STATUSES:
            raise ValueError("unsupported validation report status")
        if self.patch_eligible and self.status == "invalid":
            raise ValueError("invalid validation reports cannot be patch eligible")
        for value, field_name in (
            (self.source_text_sha256, "source_text_sha256"),
            (self.translated_text_sha256, "translated_text_sha256"),
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
        if any(not isinstance(issue, TranslationValidationIssue) for issue in self.issues):
            raise TypeError("issues must contain TranslationValidationIssue values")

    @property
    def error_count(self):
        return sum(1 for issue in self.issues if issue.severity == "error")

    @property
    def warning_count(self):
        return sum(1 for issue in self.issues if issue.severity == "warning")

    @property
    def review_count(self):
        return sum(1 for issue in self.issues if issue.severity == "review")

    def to_dict(self):
        return {
            "validator_version": TRANSLATION_VALIDATOR_VERSION,
            "unit_id": self.unit_id,
            "segment_id": self.segment_id,
            "candidate_id": self.candidate_id,
            "source_text_sha256": self.source_text_sha256,
            "translated_text_sha256": self.translated_text_sha256,
            "status": self.status,
            "patch_eligible": self.patch_eligible,
            "expected_token_count": self.expected_token_count,
            "actual_token_count": self.actual_token_count,
            "preserved_token_count": self.preserved_token_count,
            "newline_count_source": self.newline_count_source,
            "newline_count_translated": self.newline_count_translated,
            "issue_counts": dict(sorted(self.issue_counts.items())),
            "terminology_version": self.terminology_version,
            "severity_counts": {
                "error": self.error_count,
                "warning": self.warning_count,
                "review": self.review_count,
            },
            "issues": [issue.to_dict() for issue in self.issues],
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class TranslationBatchValidationReport:
    """Aggregate validation output for a candidate collection."""

    reports: tuple[TranslationValidationReport, ...]
    status: str
    patch_eligible: bool

    def __post_init__(self):
        if self.status not in VALIDATION_STATUSES:
            raise ValueError("unsupported validation batch status")
        if any(not isinstance(report, TranslationValidationReport) for report in self.reports):
            raise TypeError("reports must contain TranslationValidationReport values")
        if self.patch_eligible and self.status == "invalid":
            raise ValueError("invalid validation batches cannot be patch eligible")

    @property
    def issue_count(self):
        return sum(len(report.issues) for report in self.reports)

    @property
    def preserved_token_count(self):
        return sum(report.preserved_token_count for report in self.reports)

    @property
    def expected_token_count(self):
        return sum(report.expected_token_count for report in self.reports)

    def to_dict(self):
        issue_counts = Counter()
        severity_counts = Counter()
        for report in self.reports:
            issue_counts.update(report.issue_counts)
            severity_counts.update(
                {"error": report.error_count, "warning": report.warning_count, "review": report.review_count}
            )
        return {
            "validator_version": TRANSLATION_VALIDATOR_VERSION,
            "status": self.status,
            "patch_eligible": self.patch_eligible,
            "unit_count": len(self.reports),
            "issue_count": self.issue_count,
            "expected_token_count": self.expected_token_count,
            "preserved_token_count": self.preserved_token_count,
            "issue_counts": dict(sorted(issue_counts.items())),
            "severity_counts": {
                name: severity_counts.get(name, 0)
                for name in ("error", "warning", "review")
            },
            "reports": [report.to_dict() for report in self.reports],
        }


def _make_issue(unit, *, code, severity="error", **kwargs):
    return TranslationValidationIssue(
        code=code,
        severity=severity,
        unit_id=unit.unit_id,
        segment_id=unit.segment_id,
        **kwargs,
    )


def _count(text, value):
    return text.count(value)


def _terminology_issue(unit, rule, *, code, severity=None, expected=None, actual=None):
    return _make_issue(
        unit,
        code=code,
        severity=rule.severity if severity is None else severity,
        rule_id=rule.rule_id,
        expected_count=expected,
        actual_count=actual,
        expected_sha256=_sha256(rule.target if rule.target is not None else rule.source),
        actual_sha256=_sha256(actual) if isinstance(actual, str) else None,
    )


def _validate_terminology(unit, candidate, snapshot, issues):
    """Append content-free term/name findings and return match counters."""

    if candidate.terminology_version != snapshot.version:
        issues.append(
            _make_issue(
                unit,
                code="terminology_version_mismatch",
                expected_sha256=_sha256(snapshot.version),
                actual_sha256=_sha256(candidate.terminology_version),
            )
        )

    matched_terms = 0
    for rule in snapshot.terms:
        source_count = _count(unit.source_text, rule.source)
        if rule.target is None:
            if source_count:
                issues.append(
                    _terminology_issue(
                        unit,
                        rule,
                        code="unknown_term",
                        severity="review",
                        expected=source_count,
                    )
                )
            continue

        target_count = _count(candidate.translated_text, rule.target)
        variant_counts = sum(_count(candidate.translated_text, value) for value in rule.variants)
        if source_count:
            if target_count:
                matched_terms += min(source_count, target_count)
            if target_count < source_count:
                issues.append(
                    _terminology_issue(
                        unit,
                        rule,
                        code="term_missing",
                        expected=source_count,
                        actual=target_count,
                    )
                )
            if target_count > source_count:
                issues.append(
                    _terminology_issue(
                        unit,
                        rule,
                        code="term_count_mismatch",
                        severity="review",
                        expected=source_count,
                        actual=target_count,
                    )
                )
            if variant_counts:
                issues.append(
                    _terminology_issue(
                        unit,
                        rule,
                        code="term_variant_used",
                        severity="review",
                        expected=source_count,
                        actual=target_count + variant_counts,
                    )
                )
        elif target_count or variant_counts:
            issues.append(
                _terminology_issue(
                    unit,
                    rule,
                    code="term_unexpected",
                    severity="review",
                    expected=0,
                    actual=target_count + variant_counts,
                )
            )

    for rule in snapshot.characters:
        source_count = sum(_count(unit.source_text, value) for value in rule.source_names)
        canonical_count = _count(candidate.translated_text, rule.canonical)
        variant_counts = sum(_count(candidate.translated_text, value) for value in rule.variants)
        if source_count:
            if canonical_count:
                matched_terms += min(source_count, canonical_count)
            if canonical_count == 0 and variant_counts == 0:
                issues.append(
                    _make_issue(
                        unit,
                        code="character_name_missing",
                        severity=rule.severity,
                        rule_id=rule.rule_id,
                        speaker_sha256=_sha256(rule.speaker),
                        expected_count=source_count,
                        actual_count=0,
                        expected_sha256=_sha256(rule.canonical),
                    )
                )
            if variant_counts:
                issues.append(
                    _make_issue(
                        unit,
                        code="character_name_variant",
                        severity=rule.severity,
                        rule_id=rule.rule_id,
                        speaker_sha256=_sha256(rule.speaker),
                        expected_count=canonical_count,
                        actual_count=variant_counts,
                        expected_sha256=_sha256(rule.canonical),
                    )
                )
            if canonical_count + variant_counts > source_count:
                issues.append(
                    _make_issue(
                        unit,
                        code="character_name_count_mismatch",
                        severity="review",
                        rule_id=rule.rule_id,
                        speaker_sha256=_sha256(rule.speaker),
                        expected_count=source_count,
                        actual_count=canonical_count + variant_counts,
                    )
                )
        elif canonical_count or variant_counts:
            issues.append(
                _make_issue(
                    unit,
                    code="character_name_unexpected",
                    severity="review",
                    rule_id=rule.rule_id,
                    speaker_sha256=_sha256(rule.speaker),
                    expected_count=0,
                    actual_count=canonical_count + variant_counts,
                )
            )
    return matched_terms


def _append_report_issue(report, issue):
    issues = report.issues + (issue,)
    counts = Counter(report.issue_counts)
    counts[issue.code] += 1
    has_error = any(item.severity == "error" for item in issues)
    needs_review = any(item.severity in {"warning", "review"} for item in issues)
    return replace(
        report,
        status="invalid" if has_error else "needs_review" if needs_review else "valid",
        patch_eligible=not has_error,
        issue_counts=dict(sorted(counts.items())),
        issues=issues,
    )


def _validate_stream(unit, translated_text, stream, expected, issues):
    expected_raws = [token.raw for token in expected]
    observed = _scan_tokens(translated_text, stream, expected)
    actual_raws = [item[1] for item in observed]
    expected_counter = Counter(expected_raws)
    actual_counter = Counter(actual_raws)

    if len(expected_raws) == len(actual_raws) and expected_raws != actual_raws:
        if expected_counter == actual_counter:
            issues.append(
                _make_issue(
                    unit,
                    code="token_order_changed",
                    token_kind=stream,
                    expected_count=len(expected_raws),
                    actual_count=len(actual_raws),
                    expected_sha256=_sha256("\x1f".join(expected_raws)),
                    actual_sha256=_sha256("\x1f".join(actual_raws)),
                )
            )
        else:
            modified_count = 0
            for index, (expected_raw, actual_raw) in enumerate(zip(expected_raws, actual_raws)):
                if expected_raw != actual_raw:
                    modified_count += 1
                    issues.append(
                        _make_issue(
                            unit,
                            code="token_modified",
                            token_kind=stream,
                            source_token_index=index,
                            actual_token_index=index,
                            expected_count=1,
                            actual_count=1,
                            expected_sha256=_sha256(expected_raw),
                            actual_sha256=_sha256(actual_raw),
                        )
                    )
            if modified_count == 0:
                issues.append(
                    _make_issue(
                        unit,
                        code="token_modified",
                        token_kind=stream,
                        expected_count=len(expected_raws),
                        actual_count=len(actual_raws),
                        expected_sha256=_sha256("\x1f".join(expected_raws)),
                        actual_sha256=_sha256("\x1f".join(actual_raws)),
                    )
                )
            return len(expected_raws), len(actual_raws), sum(
                1 for raw in expected_raws if raw in actual_counter
            )

    for index, raw in enumerate(expected_raws):
        actual_count = actual_counter.get(raw, 0)
        if actual_count < expected_counter[raw]:
            issues.append(
                _make_issue(
                    unit,
                    code="token_missing",
                    token_kind=stream,
                    source_token_index=index,
                    expected_count=expected_counter[raw],
                    actual_count=actual_count,
                    expected_sha256=_sha256(raw),
                    actual_sha256=_sha256("\x1f".join(actual_raws)) if actual_raws else None,
                )
            )
        elif actual_count > expected_counter[raw]:
            issues.append(
                _make_issue(
                    unit,
                    code="token_duplicated",
                    token_kind=stream,
                    source_token_index=index,
                    expected_count=expected_counter[raw],
                    actual_count=actual_count,
                    expected_sha256=_sha256(raw),
                    actual_sha256=_sha256("\x1f".join(actual_raws)),
                )
            )

    for raw, actual_count in actual_counter.items():
        if raw not in expected_counter:
            actual_index = actual_raws.index(raw)
            issues.append(
                _make_issue(
                    unit,
                    code="token_added",
                    token_kind=stream,
                    actual_token_index=actual_index,
                    expected_count=0,
                    actual_count=actual_count,
                    expected_sha256=_sha256("\x1f".join(expected_raws)) if expected_raws else None,
                    actual_sha256=_sha256(raw),
                )
            )

    preserved = sum(min(expected_counter[raw], actual_counter[raw]) for raw in expected_counter)
    return len(expected_raws), len(actual_raws), preserved


def validate_translation_candidate(
    unit: TranslationUnit,
    candidate: TranslationCandidate,
    *,
    max_line_chars: int | None = None,
    terminology_snapshot: TerminologySnapshot | None = None,
) -> TranslationValidationReport:
    """Validate one candidate without mutating it or touching the filesystem."""

    if not isinstance(unit, TranslationUnit):
        raise TypeError("unit must be a TranslationUnit")
    if not isinstance(candidate, TranslationCandidate):
        raise TypeError("candidate must be a TranslationCandidate")
    if max_line_chars is not None and (
        isinstance(max_line_chars, bool) or not isinstance(max_line_chars, int) or max_line_chars < 1
    ):
        raise ValueError("max_line_chars must be a positive integer or None")
    if terminology_snapshot is not None and not isinstance(
        terminology_snapshot, TerminologySnapshot
    ):
        raise TypeError("terminology_snapshot must be a TerminologySnapshot or None")

    issues = []
    if candidate.unit_id != unit.unit_id:
        issues.append(_make_issue(unit, code="candidate_unit_mismatch"))
    if candidate.segment_id != unit.segment_id:
        issues.append(_make_issue(unit, code="candidate_segment_mismatch"))
    if candidate.cache_key != unit.cache_key:
        issues.append(_make_issue(unit, code="candidate_cache_key_mismatch"))

    expected_count = actual_count = preserved_count = 0
    for stream, expected in (("placeholder", unit.placeholders), ("tag", unit.tags)):
        expected_part, actual_part, preserved_part = _validate_stream(
            unit, candidate.translated_text, stream, expected, issues
        )
        expected_count += expected_part
        actual_count += actual_part
        preserved_count += preserved_part

    if terminology_snapshot is not None:
        _validate_terminology(unit, candidate, terminology_snapshot, issues)

    source_newlines = _newline_count(unit.source_text)
    translated_newlines = _newline_count(candidate.translated_text)
    if source_newlines != translated_newlines:
        issues.append(
            _make_issue(
                unit,
                code="newline_count_changed",
                expected_count=source_newlines,
                actual_count=translated_newlines,
            )
        )
    elif source_newlines and _newline_style(unit.source_text) != _newline_style(candidate.translated_text):
        issues.append(
            _make_issue(
                unit,
                code="newline_style_changed",
                severity="review",
                expected_count=source_newlines,
                actual_count=translated_newlines,
                expected_sha256=_sha256(_newline_style(unit.source_text)),
                actual_sha256=_sha256(_newline_style(candidate.translated_text)),
            )
        )

    if max_line_chars is not None:
        for line_number, line in enumerate(re.split(r"\r\n|\r|\n", candidate.translated_text), 1):
            if len(line) > max_line_chars:
                issues.append(
                    _make_issue(
                        unit,
                        code="line_length_exceeded",
                        severity="review",
                        line_number=line_number,
                        expected_count=max_line_chars,
                        actual_count=len(line),
                        max_line_chars=max_line_chars,
                    )
                )

    issue_counts = Counter(issue.code for issue in issues)
    has_error = any(issue.severity == "error" for issue in issues)
    needs_review = any(issue.severity in {"warning", "review"} for issue in issues)
    status = "invalid" if has_error else "needs_review" if needs_review else "valid"
    return TranslationValidationReport(
        unit_id=unit.unit_id,
        segment_id=unit.segment_id,
        candidate_id=candidate.candidate_id,
        source_text_sha256=unit.source_text_sha256,
        translated_text_sha256=candidate.translated_text_sha256,
        status=status,
        patch_eligible=not has_error,
        expected_token_count=expected_count,
        actual_token_count=actual_count,
        preserved_token_count=preserved_count,
        newline_count_source=source_newlines,
        newline_count_translated=translated_newlines,
        issue_counts=dict(sorted(issue_counts.items())),
        issues=tuple(issues),
        terminology_version=(
            terminology_snapshot.version if terminology_snapshot is not None else None
        ),
    )


def validate_translation_candidates(
    units,
    candidates,
    *,
    max_line_chars: int | None = None,
    terminology_snapshot: TerminologySnapshot | None = None,
) -> TranslationBatchValidationReport:
    """Validate a complete unit/candidate collection as one patch gate."""

    units = tuple(units)
    candidates = tuple(candidates)
    if any(not isinstance(unit, TranslationUnit) for unit in units):
        raise TypeError("units must contain TranslationUnit values")
    if any(not isinstance(candidate, TranslationCandidate) for candidate in candidates):
        raise TypeError("candidates must contain TranslationCandidate values")
    if terminology_snapshot is not None and not isinstance(
        terminology_snapshot, TerminologySnapshot
    ):
        raise TypeError("terminology_snapshot must be a TerminologySnapshot or None")
    if len({unit.unit_id for unit in units}) != len(units):
        raise ValueError("units must not contain duplicate unit IDs")
    if len({candidate.unit_id for candidate in candidates}) != len(candidates):
        raise ValueError("candidates must not contain duplicate unit IDs")

    candidate_by_id = {candidate.unit_id: candidate for candidate in candidates}
    reports = []
    for unit in units:
        candidate = candidate_by_id.get(unit.unit_id)
        if candidate is None:
            reports.append(
                TranslationValidationReport(
                    unit_id=unit.unit_id,
                    segment_id=unit.segment_id,
                    candidate_id="missing",
                    source_text_sha256=unit.source_text_sha256,
                    translated_text_sha256=_sha256(""),
                    status="invalid",
                    patch_eligible=False,
                    expected_token_count=len(unit.placeholders) + len(unit.tags),
                    actual_token_count=0,
                    preserved_token_count=0,
                    newline_count_source=_newline_count(unit.source_text),
                    newline_count_translated=0,
                    issue_counts={"candidate_missing": 1},
                    issues=(
                        _make_issue(unit, code="candidate_missing"),
                    ),
                )
            )
        else:
            reports.append(
                validate_translation_candidate(
                    unit,
                    candidate,
                    max_line_chars=max_line_chars,
                    terminology_snapshot=terminology_snapshot,
                )
            )

    extra_candidates = [candidate for candidate in candidates if candidate.unit_id not in {unit.unit_id for unit in units}]
    if extra_candidates:
        # Keep the aggregate conservative without copying any candidate text.
        for candidate in extra_candidates:
            reports.append(
                TranslationValidationReport(
                    unit_id=candidate.unit_id,
                    segment_id=candidate.segment_id,
                    candidate_id=candidate.candidate_id,
                    source_text_sha256=_sha256(""),
                    translated_text_sha256=candidate.translated_text_sha256,
                    status="invalid",
                    patch_eligible=False,
                    expected_token_count=0,
                    actual_token_count=0,
                    preserved_token_count=0,
                    newline_count_source=0,
                    newline_count_translated=_newline_count(candidate.translated_text),
                    issue_counts={"candidate_extra": 1},
                    issues=(
                        TranslationValidationIssue(
                            code="candidate_extra",
                            severity="error",
                            unit_id=candidate.unit_id,
                            segment_id=candidate.segment_id,
                        ),
                    ),
                )
            )

    if terminology_snapshot is not None:
        rules_by_speaker = {rule.speaker: rule for rule in terminology_snapshot.characters}
        names_by_speaker = {speaker: set() for speaker in rules_by_speaker}
        for unit in units:
            candidate = candidate_by_id.get(unit.unit_id)
            if candidate is None:
                continue
            rule = rules_by_speaker.get(unit.speaker)
            if rule is None:
                continue
            for name in (rule.canonical,) + rule.variants:
                if name in candidate.translated_text:
                    names_by_speaker[rule.speaker].add(name)
        unit_by_id = {unit.unit_id: unit for unit in units}
        for index, report in enumerate(reports):
            unit = unit_by_id.get(report.unit_id)
            if unit is None or unit.speaker not in names_by_speaker:
                continue
            if len(names_by_speaker[unit.speaker]) > 1:
                rule = rules_by_speaker[unit.speaker]
                reports[index] = _append_report_issue(
                    report,
                    _make_issue(
                        unit,
                        code="character_name_inconsistent",
                        severity=rule.severity,
                        rule_id=rule.rule_id,
                        speaker_sha256=_sha256(rule.speaker),
                        expected_count=1,
                        actual_count=len(names_by_speaker[unit.speaker]),
                        expected_sha256=_sha256(rule.canonical),
                    ),
                )

    has_error = any(report.status == "invalid" for report in reports)
    needs_review = any(report.status == "needs_review" for report in reports)
    return TranslationBatchValidationReport(
        reports=tuple(reports),
        status="invalid" if has_error else "needs_review" if needs_review else "valid",
        patch_eligible=not has_error,
    )


def candidate_acceptance_status(report: TranslationValidationReport) -> str:
    """Map validation output to the non-mutating candidate status gate."""

    if not isinstance(report, TranslationValidationReport):
        raise TypeError("report must be a TranslationValidationReport")
    if report.status == "invalid":
        return "rejected"
    if report.status == "needs_review":
        return "review"
    return "accepted"


__all__ = [
    "TRANSLATION_VALIDATOR_VERSION",
    "TranslationBatchValidationReport",
    "TranslationValidationIssue",
    "TranslationValidationReport",
    "candidate_acceptance_status",
    "validate_translation_candidate",
    "validate_translation_candidates",
]
