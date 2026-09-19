"""Table-driven tests for the PII scrubber."""

from __future__ import annotations

import pytest

from agent_prompt_capture.pii import ScrubResult, luhn_ok, scrub, scrub_path

# ---------------------------------------------------------------------------
# positives: (label, text, category, must-not-appear substring)
# ---------------------------------------------------------------------------

POSITIVES: list[tuple[str, str, str, str]] = [
    ("email simple", "mail me at alice@example.com ok", "email", "alice@example.com"),
    ("email plus tag", "use bob+ci@sub.example.co.uk", "email", "bob+ci@sub.example.co.uk"),
    ("email uppercase", "Contact ALICE@EXAMPLE.COM", "email", "ALICE@EXAMPLE.COM"),
    ("email in sentence", "Ask dana.k@corp.io about it.", "email", "dana.k@corp.io"),
    ("phone us dashes", "call 415-555-2671 now", "phone", "415-555-2671"),
    ("phone us parens", "call (415) 555-2671 now", "phone", "555-2671"),
    ("phone e164", "ring +14155552671 later", "phone", "+14155552671"),
    ("phone uk spaced", "dial +44 20 7946 0958 today", "phone", "7946"),
    ("phone dots", "fax 555.123.4567 please", "phone", "555.123.4567"),
    ("phone local 7", "call 555-2671 now", "phone", "555-2671"),
    ("ipv4", "host at 192.168.10.42 down", "ipv4", "192.168.10.42"),
    ("ipv4 public", "ping 8.8.4.4 ok", "ipv4", "8.8.4.4"),
    ("ipv6 full", "addr 2001:0db8:85a3:0000:0000:8a2e:0370:7334 up", "ipv6", "8a2e"),
    ("ipv6 compressed", "addr fe80::1ff:fe23:4567:890a up", "ipv6", "fe80::"),
    ("mac colon", "nic 00:1b:44:11:3a:b7 up", "mac_address", "00:1b:44:11:3a:b7"),
    ("mac dash", "nic 00-1B-44-11-3A-B7 up", "mac_address", "00-1B-44-11-3A-B7"),
    ("ssn", "ssn 123-45-6789 on file", "ssn", "123-45-6789"),
    ("credit card visa", "card 4111111111111111 charged", "credit_card", "4111111111111111"),
    ("credit card spaced", "card 4111 1111 1111 1111 ok", "credit_card", "4111 1111"),
    ("credit card amex", "card 378282246310005 ok", "credit_card", "378282246310005"),
    ("credit card dashes", "card 5555-5555-5555-4444 ok", "credit_card", "5555-5555"),
    ("iban gb", "pay to GB82WEST12345698765432 today", "iban", "GB82WEST12345698765432"),
    ("iban de", "pay to DE89370400440532013000 today", "iban", "DE89370400440532013000"),
    ("api key openai", "key sk-abc123456789012345678901234567890 set", "api_key", "sk-abc1234"),
    ("api key anthropic", "key sk-ant-api03-AAAAbbbbCCCCddddEEEE1234 set", "api_key", "sk-ant-"),
    ("api key proj", "key sk-proj-AAAAbbbbCCCCddddEEEE1234 set", "api_key", "sk-proj-"),
    ("api key ghp", "token ghp_AAAAbbbbCCCCddddEEEEffff1234 ok", "api_key", "ghp_"),
    ("api key gho", "token gho_AAAAbbbbCCCCddddEEEEffff1234 ok", "api_key", "gho_"),
    ("api key github pat", "pat github_pat_11ABCDEFG0abcdefghijkl ok", "api_key", "github_pat_"),
    ("api key gitlab", "pat glpat-AAAAbbbbCCCCddddEEEE ok", "api_key", "glpat-"),
    ("api key slack", "tok xoxb-123456789012-abcdefghij ok", "api_key", "xoxb-"),
    ("api key aws", "id AKIAIOSFODNN7EXAMPLE used", "api_key", "AKIAIOSFODNN7EXAMPLE"),
    ("api key google", "key AIzaSyB1234567890abcdefghijklmnopqrstuv used", "api_key", "AIzaSy"),
    ("api key npm", "tok npm_abcdefghijklmnopqrstuvwxyz0123456789 ok", "api_key", "npm_"),
    ("api key pypi", "tok pypi-AgEIcHlwaS5vcmc1234567 ok", "api_key", "pypi-"),
    ("api key hf", "tok hf_AAAAbbbbCCCCddddEEEEffff ok", "api_key", "hf_"),
    ("api key bearer", "Authorization: Bearer abcdef0123456789xyz", "api_key", "abcdef0123456789"),
    ("api key assignment", 'password = "hunter2hunter2"', "api_key", "hunter2hunter2"),
    ("api key colon", "api_key: 9f8e7d6c5b4a3210", "api_key", "9f8e7d6c5b4a3210"),
    ("api key secret", "SECRET=supersecretvalue123", "api_key", "supersecretvalue123"),
    ("jwt", "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dBjftJeZ4CVPmB92K27u", "jwt", "eyJhbGci"),
    (
        "private key",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj3\n-----END RSA PRIVATE KEY-----",
        "private_key",
        "MIIBOgIBAAJBAKj3",
    ),
    (
        "url credentials",
        "clone https://alice:s3cr3t@github.com/org/repo.git",
        "url_credentials",
        "alice:s3cr3t",
    ),
    (
        "url credentials postgres",
        "DSN postgres://dbuser:dbpass@db.internal/app",
        "url_credentials",
        "dbuser:dbpass",
    ),
    (
        "stripe secret",
        "key " + "sk_live_" + "51HabcdefghijklmnopqrstuvA" + " set",
        "api_key",
        "sk_live_",
    ),
    (
        "stripe publishable",
        "key " + "pk_live_" + "51Habcdefghijklmnopqrstuv" + " set",
        "api_key",
        "pk_live_",
    ),
    (
        "twilio sid",
        "sid " + "AC" + "1234567890abcdef1234567890abcdef" + " ok",
        "api_key",
        "AC12345678",
    ),
    (
        "sendgrid",
        "key " + "SG." + "abcdefghijklmnopqrstuv.abcdefghijklmnopqrstuvwxyz1234567890AB" + " ok",
        "api_key",
        "SG.abcdefghijklmnopqrstuv",
    ),
    ("google oauth", "tok ya29.a0AfH6SMBx1234567890abcdefghijklmnop ok", "api_key", "ya29."),
    ("prefixed password", "DB_PASSWORD = 's3cret-value'", "api_key", "s3cret-value"),
    ("prefixed secret", "STRIPE_SECRET=supersecretvalue123", "api_key", "supersecretvalue123"),
    ("x auth token header", "X-Auth-Token: abcdef123456", "api_key", "abcdef123456"),
    (
        "slack webhook",
        "post to https://hooks.slack.com/"
        + "services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
        "webhook_url",
        "T00000000",
    ),
    (
        "discord webhook",
        "post to https://discord.com/api/" + "webhooks/123456789012345678/abcdefgh_ijklmnop",
        "webhook_url",
        "abcdefgh_ijklmnop",
    ),
    (
        "ssh public key",
        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC7vbqajDhA5Vd0123456789abcdefgh alice@laptop",
        "ssh_key",
        "AAAAB3NzaC1yc2E",
    ),
    (
        "ssh ed25519 key",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBabcdefghijklmnopqrstuvwxyz0123",
        "ssh_key",
        "AAAAC3NzaC1lZDI1",
    ),
    ("uk postcode", "ship to 12 Baker Street, London NW1 6XE", "uk_postcode", "NW1 6XE"),
    ("ipv6 zone id", "ping fe80::1ff:fe23:4567:890a%eth0 now", "ipv6", "eth0"),
    ("home path macos", "open /Users/alice/dev/proj", "home_path", "/Users/alice"),
    ("home path linux", "open /home/bob/dev/proj", "home_path", "/home/bob"),
    ("home path windows", r"open C:\Users\carol\dev", "home_path", r"C:\Users\carol"),
]


@pytest.mark.parametrize(
    ("label", "text", "category", "leak"),
    POSITIVES,
    ids=[p[0] for p in POSITIVES],
)
def test_positive_detections(label, text, category, leak):
    result = scrub(text)
    assert category in result.findings, f"{label}: {category} not found in {result.findings}"
    assert leak not in result.text, f"{label}: raw value leaked -> {result.text}"


# ---------------------------------------------------------------------------
# negatives: text that must come back byte-for-byte identical
# ---------------------------------------------------------------------------

NEGATIVES: list[tuple[str, str]] = [
    ("semver", "we shipped version 1.2.3 yesterday"),
    ("semver v prefix", "bump to v10.4.11 in the lockfile"),
    ("windows version", "runs on 10.0.19041.1 here"),
    ("git sha short", "revert a3f5c1d please"),
    ("git sha long", "revert 9f8b2c1d4e5a6b7c8d9e0f1a2b3c4d5e6f708192 please"),
    ("uuid", "id 550e8400-e29b-41d4-a716-446655440000 lookup"),
    ("uuid upper", "id 550E8400-E29B-41D4-A716-446655440000 lookup"),
    ("user at host no tld", "ssh user@host works"),
    ("at handle", "ping @alice in the channel"),
    ("code call", "call foo.bar() then baz.qux()"),
    ("code member chain", "res = a.b.c.d.method()"),
    ("timestamp iso", "logged at 2026-09-19T20:11:03.123Z exactly"),
    ("timestamp time", "started 20:11:03 and ended 21:04:59"),
    ("date only", "due 2026-09-19 sharp"),
    ("date slashes", "due 09/19/2026 sharp"),
    ("port number", "serving on port 3000 now"),
    ("localhost port", "open localhost:3000 in the browser"),
    ("loopback ipv4 url", "curl http://127.0.0.1:8000/v1/health"),
    ("loopback ipv4 bare", "bound to 127.0.0.1 here"),
    ("loopback ipv4 high octet", "bound to 127.1.2.3 here"),
    ("bind all address", "serving on 0.0.0.0:8080 now"),
    ("bind all bare", "listen 0.0.0.0 everywhere"),
    ("loopback ipv6", "listen on ::1 please"),
    ("loopback ipv6 bracketed", "open http://[::1]:3000 now"),
    ("loopback ipv6 expanded", "listen on 0:0:0:0:0:0:0:1 please"),
    ("unspecified ipv6", "bind to :: for all"),
    ("unix timestamp", "the timestamp was 1758312345 exactly"),
    ("unix timestamp ms", "the timestamp was 1758312345678 exactly"),
    ("cjk", "このプロンプトは変更されない 中文测试 한국어 테스트"),
    ("lowercase postcode", "the variable nw1 6xe is not a postcode"),
    ("ssh command", "run ssh-keygen -t ed25519 -C you@example"),
    ("hex colour", "background is #ff00aa today"),
    ("hex colour short", "use #fff for the border"),
    ("cpp scope", "call std::vector<int>::size here"),
    ("prose", "Please refactor the payment module and add retries with backoff."),
    ("prose numbers", "We had 42 failures out of 1000 requests this week."),
    ("file path relative", "edit src/agent_prompt_capture/store.py now"),
    ("http url", "see https://example.com/docs/page?x=1 for details"),
    ("markdown", "- [ ] write the tests\n- [x] read the spec"),
    ("json snippet", '{"limit": 50, "offset": 0, "order": "desc"}'),
    ("sql", "SELECT count(*) FROM prompts WHERE ts >= ?"),
    ("percentages", "latency improved 12.5% over 3.4 seconds"),
    ("money", "it costs 19.99 per seat"),
    ("short digits", "issue 12345 is closed"),
    ("bytes", "allocate 1024 1048576 bytes"),
    ("range", "lines 100-200 need work"),
    ("regex literal", r"match r'\d{3}-\d{2}-\d{4}' in the parser"),
    ("env var", "read $HOME and $XDG_CONFIG_HOME at startup"),
    ("shebang", "#!/usr/bin/env python3"),
    ("import", "from collections.abc import Iterable, Sequence"),
    ("decimal version list", "supports 3.11, 3.12 and 3.13"),
    ("invalid card luhn", "card 4111111111111112 was declined"),
    ("invalid iban", "ref GB00WEST12345698765432 is wrong"),
    ("ipv4 out of range", "value 999.999.999.999 is not an address"),
    ("word with numbers", "see rfc1918 and rfc4291 for details"),
    ("ratio", "a 16:9 aspect ratio"),
    ("time range", "between 9:00 and 17:30"),
    ("empty-ish", "   "),
]


@pytest.mark.parametrize(("label", "text"), NEGATIVES, ids=[n[0] for n in NEGATIVES])
def test_negative_detections(label, text):
    result = scrub(text)
    assert result.text == text, f"{label}: text was modified -> {result.text}"
    assert result.findings == {}, f"{label}: unexpected findings {result.findings}"


# ---------------------------------------------------------------------------
# behaviour
# ---------------------------------------------------------------------------


def test_returns_scrub_result():
    assert isinstance(scrub("hello"), ScrubResult)


def test_empty_input():
    assert scrub("").text == ""
    assert scrub(None).text == ""
    assert scrub(None).findings == {}


def test_same_value_same_placeholder():
    result = scrub("mail alice@example.com then alice@example.com again")
    assert result.text.count("[EMAIL_1]") == 2
    assert "[EMAIL_2]" not in result.text
    assert result.findings["email"] == 2


def test_distinct_values_get_distinct_placeholders():
    result = scrub("from a@example.com to b@example.com")
    assert "[EMAIL_1]" in result.text
    assert "[EMAIL_2]" in result.text


def test_numbering_is_per_call():
    first = scrub("x@example.com")
    second = scrub("y@example.com")
    assert first.text == second.text == "[EMAIL_1]"


def test_jwt_is_not_partly_rematched_as_phone():
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc1234567def"
    result = scrub(f"here is {token} done")
    assert result.text == "here is [JWT_1] done"
    assert "phone" not in result.findings


def test_api_key_wins_over_overlapping_matches():
    result = scrub("key sk-ant-api03-1234567890abcdefghij and mail a@b.com")
    assert result.findings["api_key"] == 1
    assert result.findings["email"] == 1
    assert "sk-ant" not in result.text


def test_url_credentials_keep_the_host():
    result = scrub("https://alice:hunter2@example.com/repo.git")
    assert result.text.startswith("https://[URL_CREDENTIALS_1]@example.com")


def test_private_key_block_collapses_to_one_placeholder():
    text = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmU\n"
        "AAAAAAAAAAEAAAAzAAAAC3NzaC1lZDI1\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    result = scrub(f"key:\n{text}\nthanks")
    assert result.text == "key:\n[PRIVATE_KEY_1]\nthanks"
    assert result.findings == {"private_key": 1}


def test_placeholders_stay_on_one_line():
    result = scrub("a@b.com and 4111111111111111 and 192.168.1.1")
    for line in result.text.splitlines():
        assert "\n" not in line


def test_extra_terms_are_word_bounded_and_case_insensitive():
    result = scrub("Acme and acme but not Acmeish", extra_terms=["acme"])
    assert result.findings["user_term"] == 2
    assert "Acmeish" in result.text
    assert result.text.count("[USER_TERM_1]") == 2


def test_extra_terms_ignore_blank_entries():
    result = scrub("hello world", extra_terms=["", "   "])
    assert result.text == "hello world"


def test_extra_patterns():
    result = scrub("ticket PROJ-1234 filed", extra_patterns=[r"PROJ-\d+"])
    assert result.findings["custom"] == 1
    assert "PROJ-1234" not in result.text


def test_invalid_extra_pattern_is_ignored():
    result = scrub("hello", extra_patterns=["([unclosed"])
    assert result.text == "hello"


def test_ner_absent_does_not_crash(monkeypatch):
    import agent_prompt_capture.pii as pii

    monkeypatch.setattr(pii, "_NER_ENGINE", None)
    monkeypatch.setattr(pii, "_NER_FAILED", False)
    result = scrub("Alice went to Paris", enable_ner=True)
    assert isinstance(result, ScrubResult)
    assert "Alice" in result.text or "[PERSON_1]" in result.text


def test_findings_counts_occurrences():
    result = scrub("a@x.com b@x.com key sk-abc123456789012345678901234")
    assert result.findings["email"] == 2
    assert result.findings["api_key"] == 1


# ---------------------------------------------------------------------------
# luhn + scrub_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("digits", "expected"),
    [
        ("4111111111111111", True),
        ("5555555555554444", True),
        ("378282246310005", True),
        ("6011111111111117", True),
        ("4111111111111112", False),
        ("1234567890123", False),
        ("", False),
        ("abcd", False),
        ("411111111111", False),
    ],
)
def test_luhn(digits, expected):
    assert luhn_ok(digits) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/Users/alice/x", "/Users/[USER]/x"),
        ("/Users/alice", "/Users/[USER]"),
        ("/home/bob/dev/proj", "/home/[USER]/dev/proj"),
        (r"C:\Users\carol\dev", r"C:\Users\[USER]\dev"),
        ("/Users/Shared/data", "/Users/Shared/data"),
        ("/opt/app", "/opt/app"),
        ("", ""),
        (None, None),
        ("/Users/[USER]/x", "/Users/[USER]/x"),
    ],
)
def test_scrub_path(raw, expected):
    assert scrub_path(raw) == expected


def test_four_part_version_is_indistinguishable_from_an_ipv4():
    """Documented ambiguity: ``1.2.3.4`` is a valid dotted quad, so it is redacted.

    Version strings that cannot be a dotted quad (an octet above 255) survive.
    """
    assert scrub("the build is 1.2.3.4 exactly").findings == {"ipv4": 1}
    assert scrub("runs on 10.0.19041.1 here").findings == {}


def test_scrub_path_is_idempotent():
    once = scrub_path("/Users/alice/dev")
    assert scrub_path(once) == once


# ---------------------------------------------------------------------------
# regressions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "11111111-2222-3333-4444-555555555555",
        "550e8400-e29b-41d4-a716-446655440000",
        "GET https://claude.ai/chat/11111111-2222-3333-4444-555555555555 now",
        "conversation_id=11111111-2222-3333-4444-555555555555&x=1",
        "see /tmp/11111111-2222-3333-4444-555555555555.jsonl for the transcript",
        "revert 9f8b2c1d4e5a6b7c8d9e0f1a2b3c4d5e6f708192 please",
        "revert 1234567890123456789012345678901234567890 please",
        "id 4111111111111111-suffix stays",
        "key prefix-4111111111111111 stays",
    ],
)
def test_identifiers_are_never_mistaken_for_a_card(text):
    """A UUID's tail (``-4444-555555555555``) is 16 digits and Luhn-valid.

    Every browser ``conversation_id`` is a UUID and becomes our ``session_id``, so a
    card false positive here silently corrupts stored session ids.
    """
    result = scrub(text)
    assert result.text == text, f"text was modified -> {result.text}"
    assert "credit_card" not in result.findings


@pytest.mark.parametrize(
    "text",
    [
        "card 4111111111111111 charged",
        "card 4111 1111 1111 1111 ok",
        "card 4111-1111-1111-1111 ok",
        "card 5555-5555-5555-4444 ok",
    ],
)
def test_a_standalone_card_is_still_caught(text):
    result = scrub(text)
    assert result.findings.get("credit_card") == 1
    assert "4111" not in result.text
    assert "5555" not in result.text


def test_uuid_survives_alongside_real_pii():
    text = "session 550e8400-e29b-41d4-a716-446655440000 for alice@example.com"
    result = scrub(text)
    assert "550e8400-e29b-41d4-a716-446655440000" in result.text
    assert result.findings == {"email": 1}


def test_loopback_survives_but_private_addresses_do_not():
    text = "curl http://127.0.0.1:8000/ then http://192.168.1.50:8000/"
    result = scrub(text)
    assert "127.0.0.1:8000" in result.text
    assert "192.168.1.50" not in result.text
    assert result.findings == {"ipv4": 1}


def test_localhost_and_bind_all_with_ports_survive():
    text = "listening on localhost:3000, 0.0.0.0:8080 and [::1]:5432"
    assert scrub(text).text == text


def test_new_categories_are_all_wired_into_scrub():
    """Every name in CATEGORIES must be reachable, or the docs lie about it."""
    from agent_prompt_capture.pii import CATEGORIES

    samples = {
        "ssh_key": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC7vbqajDhA5Vd01234567 me@host",
        "webhook_url": "https://hooks.slack.com/services/T0/B0/XXXXXXXXXXXXXXXXXXXXXXXX",
        "uk_postcode": "London NW1 6XE",
    }
    for category, text in samples.items():
        assert category in CATEGORIES
        assert category in scrub(text).findings, f"{category} is listed but never fires"


def test_placeholder_numbering_is_stable_left_to_right():
    result = scrub("b@x.com then a@x.com then b@x.com again")
    assert result.text == "[EMAIL_1] then [EMAIL_2] then [EMAIL_1] again"


def test_overlapping_spans_resolve_to_the_outermost_match():
    """``url_credentials`` starts before the key inside it, so it wins the whole span."""
    result = scrub("git remote add origin https://u:ghp_AAAAbbbbCCCCddddEEEE@github.com/o/r")
    assert result.text.endswith("@github.com/o/r")
    assert "ghp_" not in result.text
    assert result.findings == {"url_credentials": 1}


def test_long_input_is_not_pathological():
    import time

    text = ("refactor the module and call alice@example.com about it\n" * 4000)[:200_000]
    started = time.monotonic()
    result = scrub(text)
    assert time.monotonic() - started < 5.0
    assert "[EMAIL_1]" in result.text
