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

__all__ = ["ScrubResult", "scrub", "scrub_path", "CATEGORIES", "luhn_ok", "MAX_SCRUB_CHARS"]

#: Hard upper bound on what is handed to :func:`scrub`. It matches the HTTP listener's
#: 1 MiB body limit, so a payload that arrived over the wire is scrubbed whole; only a
#: pathological local paste is cut, and the span machinery is O(patterns x text).
#: It is deliberately larger than what callers store (``ingest.MAX_PROMPT_CHARS``), so a
#: secret straddling the stored cap is still recognised and replaced before truncation.
MAX_SCRUB_CHARS = 1_048_576

_log = logging.getLogger("agent_prompt_capture")

#: Category names in application order, most specific first.
CATEGORIES: tuple[str, ...] = (
    "private_key",
    "ssh_key",
    "jwt",
    "webhook_url",
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
    "uk_postcode",
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


#: Registered IBAN length per country (ISO 13616). A country we do not know about
#: falls back to the generic 15-34 range, so an unlisted country is never worse off.
_IBAN_LENGTHS: dict[str, int] = {
    entry[:2]: int(entry[2:])
    for entry in (
        "AD24 AE23 AL28 AT20 AZ28 BA20 BE16 BG22 BH22 BI27 BR29 BY28 "
        "CH21 CR22 CY28 CZ24 DE22 DJ27 DK18 DO28 EE20 EG29 ES24 FI18 "
        "FO18 FR27 GB22 GE22 GI23 GL18 GR27 GT28 HR21 HU28 IE22 IL23 "
        "IQ23 IS26 IT27 JO30 KW30 KZ20 LB28 LC32 LI21 LT20 LU20 LV21 "
        "LY25 MC27 MD24 ME22 MK19 MR27 MT31 MU30 NI28 NL18 NO15 PK24 "
        "PL28 PS29 PT25 QA29 RO24 RS22 RU33 SA24 SC31 SD18 SE24 SI19 "
        "SK24 SM27 ST25 SV28 TL23 TN24 TR26 UA29 VA22 VG24 XK20"
    ).split()
}


def _iban_ok(text: str) -> bool:
    value = text.replace(" ", "").upper()
    if not 15 <= len(value) <= 34:
        return False
    expected = _IBAN_LENGTHS.get(value[:2])
    if expected is not None and len(value) != expected:
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
    """A dotted quad worth redacting.

    Loopback (``127.0.0.0/8``) and the unspecified/bind-all address (``0.0.0.0``)
    are deliberately **kept**: they identify nobody and no network, and redacting
    them mangles everyday developer text such as ``curl http://127.0.0.1:8000/``.
    Everything else, private ranges included, is redacted.
    """
    parts = text.split(".")
    if len(parts) != 4:
        return False
    if not all(p.isdigit() and len(p) <= 3 and int(p) <= 255 for p in parts):
        return False
    if int(parts[0]) == 127:  # 127.0.0.0/8 loopback
        return False
    return any(int(p) for p in parts)  # 0.0.0.0 is the unspecified address


def _ipv6_ok(text: str) -> bool:
    """Same rule as :func:`_ipv4_ok`: keep ``::1`` and ``::``, redact the rest."""
    core = text.split("%", 1)[0]
    # ``::`` on its own carries no address; require at least one hex digit.
    if not any(c in "0123456789abcdefABCDEF" for c in core):
        return False
    groups = [g for g in core.replace("::", ":").split(":") if g]
    if not groups:  # pragma: no cover - defensive, the check above covers it
        return False
    try:
        values = [int(g, 16) for g in groups]
    except ValueError:  # pragma: no cover - defensive
        return False
    # ::1 / 0:0:0:0:0:0:0:1 loopback and the unspecified address.
    return not (all(v == 0 for v in values[:-1]) and values[-1] in (0, 1))


# --------------------------------------------------------------------------
# patterns
# --------------------------------------------------------------------------

_HEX = "[0-9A-Fa-f]"

PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----"
    r".*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL,
)

#: An ``authorized_keys`` / ``id_*.pub`` line. The trailing comment is only swallowed
#: when it looks like ``user@host``, so ordinary prose after a key survives.
SSH_KEY_RE = re.compile(
    r"(?:ssh-(?:rsa|dss|ed25519)|ecdsa-sha2-nistp(?:256|384|521)"
    r"|sk-(?:ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com)"
    r"[ \t]+AAAA[0-9A-Za-z+/]{20,}={0,3}"
    r"(?:[ \t]+\S+@\S+)?"
)

JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")

#: Incoming-webhook URLs. The path *is* the credential, so the whole URL goes.
WEBHOOK_URL_RE = re.compile(
    r"(?i)https://(?:"
    r"hooks\.slack\.com/(?:services|workflows|triggers)/"
    r"|(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/"
    r"|[a-z0-9-]+\.webhook\.office\.com/webhookb2/"
    r"|outlook\.office(?:365)?\.com/webhook/"
    r"|chat\.googleapis\.com/v1/spaces/"
    r"|discord\.com/api/v\d+/webhooks/"
    r")[^\s<>\"')]+"
)

API_KEY_RES: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\bsk-(?:ant|proj|or|live|test)?-?[A-Za-z0-9_-]{16,}"), 0),
    # Stripe (and the many services that copied its ``<prefix>_<env>_<blob>`` shape).
    (re.compile(r"\b[a-z]{2,4}_(?:live|test)_[A-Za-z0-9]{16,}"), 0),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), 0),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), 0),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), 0),
    (re.compile(r"\bxox[abpsr]-[A-Za-z0-9-]{10,}"), 0),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), 0),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    (re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"), 0),  # Google OAuth access token
    (re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"), 0),  # SendGrid
    (re.compile(r"\bAC[0-9a-fA-F]{32}\b"), 0),  # Twilio account SID
    (re.compile(r"\bSK[0-9a-fA-F]{32}\b"), 0),  # Twilio API key SID
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), 0),
    (re.compile(r"\bpypi-[A-Za-z0-9_-]{16,}"), 0),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), 0),
    # ``bearer``/``BEARER`` are as valid as ``Bearer`` in an Authorization header.
    (re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]{12,})"), 1),
    (
        # ``password = "x"`` and friends. The key name may carry any number of
        # prefix *and* suffix segments (``DB_PASSWORD``, ``STRIPE_SECRET``,
        # ``X-Auth-Token``, ``AWS_SECRET_ACCESS_KEY``), and may be quoted as a JSON
        # key (``"api_key": "..."``), so the quote before the separator is optional.
        re.compile(
            r"(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_.-])*"
            r"(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|refresh[_-]?token"
            r"|client[_-]?secret|secret|token|password|passwd|passphrase)"
            r"(?:[_.-][A-Za-z0-9]+)*"
            r"\b[\"']?\s*[:=]\s*[\"']?([^\s\"',;)]{6,})[\"']?"
        ),
        1,
    ),
)

URL_CREDENTIALS_RE = re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]+(?=@)")

#: 13-19 digits, optionally grouped. The guards matter more than the pattern: without
#: them the tail of a UUID (``...-4444-555555555555``) is 16 digits and Luhn-valid, and
#: every browser ``conversation_id`` is a UUID that ends up as our ``session_id``.
CREDIT_CARD_RE = re.compile(r"(?<![\w.-])(?:\d[ -]?){12,18}\d(?![\w-])")

#: Anything inside a UUID is an identifier, never a card/phone/SSN/IBAN.
UUID_RE = re.compile(
    r"(?<![0-9A-Za-z-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9A-Za-z-])"
)

#: Categories that a UUID must never be mistaken for.
_UUID_EXEMPT: frozenset[str] = frozenset({"credit_card", "phone", "ssn", "iban"})

#: An IBAN, written solid (``GB33BUKB20201555555555``) or in the printed form with
#: grouping spaces (``GB33 BUKB 2020 1555 5555 55``). The grouped alternative is greedy
#: and will happily swallow a short uppercase word that follows the number, so the
#: candidate it yields is validated (and, failing that, shortened group by group) by
#: :func:`_iban_spans` rather than by a plain ``validator=`` on :func:`_add`.
IBAN_RE = re.compile(
    r"\b[A-Z]{2}\d{2}"
    r"(?:[A-Z0-9]{11,30}|(?:[ ][A-Z0-9]{4})+(?:[ ][A-Z0-9]{1,4})?)"
    r"\b"
)

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
    r")(?:%[A-Za-z0-9_.-]+)?(?![\w:.])"  # keep the zone id inside the placeholder
)

IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")

MAC_RE = re.compile(rf"(?<![\w:.-])(?:{_HEX}{{2}}[:-]){{5}}{_HEX}{{2}}(?![\w:.-])")

#: A UK postcode in its canonical uppercase, space-separated form ("NW1 6XE").
#: Uppercase-only and single-space-only on purpose: the lowercase/unspaced variants
#: collide with far too many identifiers to be worth it.
UK_POSTCODE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,2}\d[A-Z\d]? \d[A-Z]{2}(?![A-Za-z0-9])")

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


def _iban_spans(text: str) -> list[_Span]:
    """IBAN candidates, shortened group by group until the mod-97 check passes.

    ``(?:[ ][A-Z0-9]{4})+(?:[ ][A-Z0-9]{1,4})?`` is greedy, so a valid grouped IBAN
    followed by a short uppercase word (``ES91 ... 1332 OPEN``, ``... POST``, ``... OK``)
    matches as one oversized candidate. ``finditer`` only ever yields that maximal match,
    so validating it and giving up would leave the account number in the clear. Instead
    we drop trailing space-separated groups one at a time and keep the longest prefix
    that is a well-formed IBAN. The solid form has no spaces and is tested exactly once.
    """
    spans: list[_Span] = []
    priority = _priority("iban")
    for match in IBAN_RE.finditer(text):
        candidate = match.group(0)
        while candidate:
            if _iban_ok(candidate):
                spans.append(
                    _Span(
                        match.start(),
                        match.start() + len(candidate),
                        "iban",
                        candidate,
                        priority,
                    )
                )
                break
            cut = candidate.rfind(" ")
            if cut == -1:
                break
            candidate = candidate[:cut]
    return spans


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


def _drop_uuid_overlaps(spans: list[_Span], text: str) -> list[_Span]:
    """Remove number-shaped candidates that fall inside a UUID.

    A UUID is a machine identifier: its digit runs are not cards, phone numbers,
    social security numbers or IBANs, however well they happen to check out.
    """
    if not any(span.category in _UUID_EXEMPT for span in spans):
        return spans
    regions = [match.span() for match in UUID_RE.finditer(text)]
    if not regions:
        return spans
    return [
        span
        for span in spans
        if span.category not in _UUID_EXEMPT
        or not any(span.start < end and start < span.end for start, end in regions)
    ]


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
    _add(spans, text, SSH_KEY_RE, "ssh_key")
    _add(spans, text, JWT_RE, "jwt")
    _add(spans, text, WEBHOOK_URL_RE, "webhook_url")
    for pattern, group in API_KEY_RES:
        _add(spans, text, pattern, "api_key", group=group)
    _add(spans, text, URL_CREDENTIALS_RE, "url_credentials")
    _add(spans, text, CREDIT_CARD_RE, "credit_card", validator=_credit_card_ok)
    spans.extend(_iban_spans(text))
    _add(spans, text, SSN_RE, "ssn")
    _add(spans, text, EMAIL_RE, "email")
    _add(spans, text, PHONE_RE, "phone", validator=_phone_ok)
    _add(spans, text, IPV6_RE, "ipv6", validator=_ipv6_ok)
    _add(spans, text, IPV4_RE, "ipv4", validator=_ipv4_ok)
    _add(spans, text, MAC_RE, "mac_address")
    _add(spans, text, UK_POSTCODE_RE, "uk_postcode")
    spans.extend(_home_spans(text))
    spans.extend(_term_spans(text, extra_terms))
    spans.extend(_custom_spans(text, extra_patterns))
    if enable_ner:
        spans.extend(_ner_spans(text))

    resolved = _resolve(_drop_uuid_overlaps(spans, text))
    scrubbed, findings = _apply(text, resolved, {}, {})
    return ScrubResult(scrubbed, findings)
