"""PII scrubbing.

Pure functions, no I/O, no global mutable state beyond the lazily built NER engine.
Every regex is compiled once at import time.

The scrubber works in a single pass: every rule proposes *candidate spans* over the
original text, the spans are then reconciled (earliest wins, longest wins on a tie,
then rule priority) and only the surviving spans are replaced. That is what stops a
JWT from being partially re-matched as a phone number.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

__all__ = ["ScrubResult", "scrub", "scrub_path", "CATEGORIES", "luhn_ok"]

_log = logging.getLogger("agent_prompt_capture")

#: Category names in application order, most specific first.
CATEGORIES: tuple[str, ...] = (
    "private_key",
    "jwt",
    "api_key",
    "url_credentials",
    "credit_card",
    "iban",
    "ssn",
    "email",
    "phone",
    "ipv6",
    "ipv4",
    "mac_address",
    "home_path",
    "user_term",
    "custom",
    "person",
    "location",
)

USER_PLACEHOLDER = "[USER]"

_HOME_SKIP = frozenset({"shared", "public", "default", "[user]", "all users"})


# --------------------------------------------------------------------------
# validators
# --------------------------------------------------------------------------


def luhn_ok(digits: str) -> bool:
    """Luhn checksum over a string of digits."""
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _credit_card_ok(text: str) -> bool:
    return luhn_ok(re.sub(r"[ -]", "", text))


def _iban_ok(text: str) -> bool:
    value = text.replace(" ", "").upper()
    if not 15 <= len(value) <= 34:
        return False
    rearranged = value[4:] + value[:4]
    converted = "".join(str(int(c, 36)) if c.isalpha() else c for c in rearranged)
    if not converted.isdigit():
        return False
    return int(converted) % 97 == 1


_DATE_LIKE = (
    re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$"),
    re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{4}$"),
)


def _phone_ok(text: str) -> bool:
    """Reject dates, versions and anything that is not 7-15 digits long."""
    stripped = text.strip()
    for pattern in _DATE_LIKE:
        if pattern.match(stripped):
            return False
    digits = re.sub(r"\D", "", stripped)
    if not 7 <= len(digits) <= 15:
        return False
    if "." in stripped and not stripped.startswith("+"):
        # 1.2.3.4.5.6.7 / 10.0.19041.1 style version strings are not phone numbers.
        if len(stripped.split(".")) >= 4:
            return False
    groups = re.findall(r"\d+", stripped)
    if not stripped.startswith("+") and any(len(g) > 5 for g in groups):
        return False
    return True


def _ipv4_ok(text: str) -> bool:
    parts = text.split(".")
    if len(parts) != 4:
        return False
    return all(p.isdigit() and len(p) <= 3 and int(p) <= 255 for p in parts)


def _ipv6_ok(text: str) -> bool:
    # ``::`` on its own carries no address; require at least one hex digit.
    return any(c in "0123456789abcdefABCDEF" for c in text)


# --------------------------------------------------------------------------
# patterns
# --------------------------------------------------------------------------

_HEX = "[0-9A-Fa-f]"

PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.DOTALL,
)

JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")

API_KEY_RES: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\bsk-(?:ant|proj|or|live|test)?-?[A-Za-z0-9_-]{16,}"), 0),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), 0),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), 0),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), 0),
    (re.compile(r"\bxox[abpsr]-[A-Za-z0-9-]{10,}"), 0),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), 0),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), 0),
    (re.compile(r"\bpypi-[A-Za-z0-9_-]{16,}"), 0),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), 0),
    (re.compile(r"\bBearer\s+([A-Za-z0-9._~+/=-]{12,})"), 1),
    (
        re.compile(
            r"(?i)\b(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret|token"
            r"|password|passwd)\b\s*[:=]\s*[\"']?([^\s\"',;)]{6,})[\"']?"
        ),
        1,
    ),
)

URL_CREDENTIALS_RE = re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]+(?=@)")

CREDIT_CARD_RE = re.compile(r"(?<![\d.])(?:\d[ -]?){12,18}\d(?![\d])")

IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")

SSN_RE = re.compile(r"\b(?!000|666|9\d\d)\d{3}[- ](?!00)\d{2}[- ](?!0000)\d{4}\b")

EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,24}\b"
)

PHONE_RE = re.compile(
    r"(?<![\w.+-])"
    r"(?:\+\d[\d\s().-]{5,17}\d|\(?\d{1,4}\)?(?:[\s.-]\d{2,4}){1,4})"
    r"(?![\d-])"
)

IPV6_RE = re.compile(
    r"(?<![\w:.])(?:"
    rf"(?:{_HEX}{{1,4}}:){{7}}{_HEX}{{1,4}}"
    rf"|(?:{_HEX}{{1,4}}:){{1,7}}:"
    rf"|(?:{_HEX}{{1,4}}:){{1,6}}:{_HEX}{{1,4}}"
    rf"|(?:{_HEX}{{1,4}}:){{1,5}}(?::{_HEX}{{1,4}}){{1,2}}"
    rf"|(?:{_HEX}{{1,4}}:){{1,4}}(?::{_HEX}{{1,4}}){{1,3}}"
    rf"|(?:{_HEX}{{1,4}}:){{1,3}}(?::{_HEX}{{1,4}}){{1,4}}"
    rf"|(?:{_HEX}{{1,4}}:){{1,2}}(?::{_HEX}{{1,4}}){{1,5}}"
    rf"|{_HEX}{{1,4}}:(?::{_HEX}{{1,4}}){{1,6}}"
    rf"|:(?:(?::{_HEX}{{1,4}}){{1,7}}|:)"
    r")(?![\w:.])"
)

IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")

MAC_RE = re.compile(rf"(?<![\w:.-])(?:{_HEX}{{2}}[:-]){{5}}{_HEX}{{2}}(?![\w:.-])")

HOME_PATH_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\w])(/Users/|/home/)([^/\\\s:\"'<>|,;)]+)"),
    re.compile(r"(?i)(?<![\w])([A-Za-z]:\\Users\\)([^/\\\s:\"'<>|,;)]+)"),
    re.compile(r"(?<![\w])(/root)(?![\w])"),
)


# --------------------------------------------------------------------------
# span machinery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    category: str
    value: str
    priority: int
    fixed: str | None = None


@dataclass
class ScrubResult:
    """The scrubbed text plus a per-category count of what was replaced."""

    text: str
    findings: dict[str, int] = field(default_factory=dict)

    def __iter__(self):  # pragma: no cover - convenience unpacking
        yield self.text
        yield self.findings


def _priority(category: str) -> int:
    try:
        return CATEGORIES.index(category)
    except ValueError:  # pragma: no cover - defensive
        return len(CATEGORIES)


def _add(
    spans: list[_Span],
    text: str,
    pattern: re.Pattern[str],
    category: str,
    *,
    group: int = 0,
    validator: Callable[[str], bool] | None = None,
    fixed: str | None = None,
) -> None:
    priority = _priority(category)
    for match in pattern.finditer(text):
        try:
            start, end = match.span(group)
        except IndexError:  # pragma: no cover - defensive
            continue
        if start < 0 or end <= start:
            continue
        value = text[start:end]
        if validator is not None and not validator(value):
            continue
        spans.append(_Span(start, end, category, value, priority, fixed))


def _home_spans(text: str) -> list[_Span]:
    spans: list[_Span] = []
    priority = _priority("home_path")
    for pattern in HOME_PATH_RES:
        for match in pattern.finditer(text):
            if match.re.groups < 2:
                # bare /root
                spans.append(
                    _Span(
                        match.start(1),
                        match.end(1),
                        "home_path",
                        match.group(1),
                        priority,
                        "/home/" + USER_PLACEHOLDER,
                    )
                )
                continue
            name = match.group(2)
            if name.lower() in _HOME_SKIP or name.startswith("["):
                continue
            spans.append(
                _Span(
                    match.start(2),
                    match.end(2),
                    "home_path",
                    name,
                    priority,
                    USER_PLACEHOLDER,
                )
            )
    return spans


def _term_spans(text: str, terms: Iterable[str]) -> list[_Span]:
    spans: list[_Span] = []
    priority = _priority("user_term")
    for term in terms:
        cleaned = str(term).strip()
        if not cleaned:
            continue
        pattern = re.compile(r"(?<!\w)" + re.escape(cleaned) + r"(?!\w)", re.IGNORECASE)
        for match in pattern.finditer(text):
            spans.append(_Span(match.start(), match.end(), "user_term", cleaned.lower(), priority))
    return spans


def _custom_spans(text: str, patterns: Iterable[str]) -> list[_Span]:
    spans: list[_Span] = []
    priority = _priority("custom")
    for raw in patterns:
        try:
            pattern = re.compile(str(raw))
        except re.error:
            _log.warning("ignoring invalid extra_pattern %r", raw)
            continue
        for match in pattern.finditer(text):
            if match.end() <= match.start():
                continue
            spans.append(_Span(match.start(), match.end(), "custom", match.group(0), priority))
    return spans


def _resolve(spans: Sequence[_Span]) -> list[_Span]:
    """Earliest span wins; on a tie the longest wins; then rule priority."""
    ordered = sorted(spans, key=lambda s: (s.start, -(s.end - s.start), s.priority))
    accepted: list[_Span] = []
    cursor = -1
    for span in ordered:
        if span.start < cursor:
            continue
        accepted.append(span)
        cursor = span.end
    return accepted


# --------------------------------------------------------------------------
# optional NER
# --------------------------------------------------------------------------

_NER_ENGINE: object | None = None
_NER_FAILED = False

_NER_LABELS = {"PERSON": "person", "LOCATION": "location", "GPE": "location", "NRP": "person"}


def _ner_spans(text: str) -> list[_Span]:
    """Presidio-backed name/location detection. Silently degrades when absent."""
    global _NER_ENGINE, _NER_FAILED
    if _NER_FAILED:
        return []
    if _NER_ENGINE is None:
        try:
            from presidio_analyzer import AnalyzerEngine  # noqa: PLC0415
        except Exception:  # pragma: no cover - extra not installed
            _NER_FAILED = True
            _log.warning(
                "enable_ner is set but the 'ner' extra is not installed; "
                "install agent-prompt-capture[ner] to enable it"
            )
            return []
        try:
            _NER_ENGINE = AnalyzerEngine()
        except Exception:  # pragma: no cover - model missing
            _NER_FAILED = True
            _log.warning("could not start the NER engine; continuing without it")
            return []

    try:
        results = _NER_ENGINE.analyze(  # type: ignore[attr-defined]
            text=text, entities=list(_NER_LABELS), language="en"
        )
    except Exception:  # pragma: no cover - runtime failure
        _log.warning("NER analysis failed; continuing without it")
        return []

    spans: list[_Span] = []
    for result in results:
        category = _NER_LABELS.get(getattr(result, "entity_type", ""), None)
        if not category:
            continue
        start = int(getattr(result, "start", 0))
        end = int(getattr(result, "end", 0))
        if end <= start:
            continue
        spans.append(_Span(start, end, category, text[start:end], _priority(category)))
    return spans


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def scrub_path(path: str | None) -> str | None:
    """``/Users/alice/x`` -> ``/Users/[USER]/x``. Also ``/home/...`` and ``C:\\Users\\...``."""
    if not path:
        return path
    spans = _resolve(_home_spans(path))
    return _apply(path, spans, {}, {})[0]


def _apply(
    text: str,
    spans: Sequence[_Span],
    counters: dict[str, int],
    seen: dict[tuple[str, str], str],
) -> tuple[str, dict[str, int]]:
    findings: dict[str, int] = {}
    pieces: list[str] = []
    cursor = 0
    for span in spans:
        pieces.append(text[cursor : span.start])
        if span.fixed is not None:
            replacement = span.fixed
        else:
            key = (span.category, span.value.lower())
            replacement = seen.get(key)
            if replacement is None:
                counters[span.category] = counters.get(span.category, 0) + 1
                replacement = f"[{span.category.upper()}_{counters[span.category]}]"
                seen[key] = replacement
        pieces.append(replacement)
        findings[span.category] = findings.get(span.category, 0) + 1
        cursor = span.end
    pieces.append(text[cursor:])
    return "".join(pieces), findings


def scrub(
    text: str | None,
    *,
    extra_terms: Iterable[str] = (),
    extra_patterns: Iterable[str] = (),
    enable_ner: bool = False,
) -> ScrubResult:
    """Replace every recognised PII value in ``text`` with a stable placeholder."""
    if not text:
        return ScrubResult(text or "", {})

    spans: list[_Span] = []

    _add(spans, text, PRIVATE_KEY_RE, "private_key")
    _add(spans, text, JWT_RE, "jwt")
    for pattern, group in API_KEY_RES:
        _add(spans, text, pattern, "api_key", group=group)
    _add(spans, text, URL_CREDENTIALS_RE, "url_credentials")
    _add(spans, text, CREDIT_CARD_RE, "credit_card", validator=_credit_card_ok)
    _add(spans, text, IBAN_RE, "iban", validator=_iban_ok)
    _add(spans, text, SSN_RE, "ssn")
    _add(spans, text, EMAIL_RE, "email")
    _add(spans, text, PHONE_RE, "phone", validator=_phone_ok)
    _add(spans, text, IPV6_RE, "ipv6", validator=_ipv6_ok)
    _add(spans, text, IPV4_RE, "ipv4", validator=_ipv4_ok)
    _add(spans, text, MAC_RE, "mac_address")
    spans.extend(_home_spans(text))
    spans.extend(_term_spans(text, extra_terms))
    spans.extend(_custom_spans(text, extra_patterns))
    if enable_ner:
        spans.extend(_ner_spans(text))

    resolved = _resolve(spans)
    scrubbed, findings = _apply(text, resolved, {}, {})
    return ScrubResult(scrubbed, findings)
