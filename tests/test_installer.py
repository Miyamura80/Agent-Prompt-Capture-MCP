"""Installer: idempotent merges, preserved keys, atomic writes."""

from __future__ import annotations

import json
import tomllib

import pytest

from agent_prompt_capture.installer import (
    CLAUDE_HOOK_EVENTS,
    CODEX_DEFAULT_TIMEOUT,
    CODEX_HOOK_COMMAND,
    CODEX_HOOK_EVENTS,
    HOOK_COMMAND,
    HOOK_TIMEOUT,
    OPENCODE_PLUGIN_SOURCE,
    ConfigFormatError,
    claude_settings_path,
    codex_config_path,
    codex_expected_trust,
    codex_hook_state_key,
    codex_hook_trust_hash,
    codex_hooks_path,
    install,
    opencode_plugin_path,
    uninstall,
)

# ---------------------------------------------------------------------------
# claude code
# ---------------------------------------------------------------------------


def _hook_commands(path, event):
    data = json.loads(path.read_text())
    return [
        hook["command"]
        for matcher in data["hooks"].get(event, [])
        for hook in matcher.get("hooks", [])
    ]


def test_claude_install_creates_both_hooks():
    message = install("claude-code")
    path = claude_settings_path()
    assert path.exists()
    assert "UserPromptSubmit" in message and "Stop" in message
    for event in CLAUDE_HOOK_EVENTS:
        assert _hook_commands(path, event) == [HOOK_COMMAND]


def test_claude_install_is_idempotent():
    install("claude-code")
    before = claude_settings_path().read_text()
    message = install("claude-code")
    assert claude_settings_path().read_text() == before
    assert "already installed" in message
    for event in CLAUDE_HOOK_EVENTS:
        assert _hook_commands(claude_settings_path(), event) == [HOOK_COMMAND]


def test_claude_install_preserves_unrelated_keys():
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "model": "opus",
                "env": {"FOO": "bar"},
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
                    ]
                },
            }
        )
    )
    install("claude-code")
    data = json.loads(path.read_text())
    assert data["model"] == "opus"
    assert data["env"] == {"FOO": "bar"}
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo hi"
    assert _hook_commands(path, "UserPromptSubmit") == [HOOK_COMMAND]


def test_claude_install_appends_to_existing_event():
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-tool"}]}]
                }
            }
        )
    )
    install("claude-code")
    assert _hook_commands(path, "UserPromptSubmit") == ["other-tool", HOOK_COMMAND]


def test_claude_install_dry_run_writes_nothing():
    message = install("claude-code", dry_run=True)
    assert "[dry-run]" in message
    assert HOOK_COMMAND in message
    assert not claude_settings_path().exists()


def test_claude_install_honours_claude_config_dir(monkeypatch, tmp_path):
    target = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    install("claude-code")
    assert (target / "settings.json").exists()


def test_claude_install_refuses_broken_json():
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        install("claude-code")
    assert path.read_text() == "{not json", "a file we cannot parse must be left alone"


def test_claude_install_refuses_trailing_commas_with_a_clear_message():
    """JSON5-isms are the classic way a hand-edited settings.json goes bad."""
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = '{\n  "hooks": {},\n}\n'
    path.write_text(original)
    with pytest.raises(ValueError) as excinfo:
        install("claude-code")
    message = str(excinfo.value)
    assert "not valid JSON" in message
    assert "trailing commas" in message
    assert str(path) in message
    assert path.read_text() == original


def test_claude_install_refuses_a_non_object_document():
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('["not", "an", "object"]')
    with pytest.raises(ValueError, match="not an object"):
        install("claude-code")
    assert json.loads(path.read_text()) == ["not", "an", "object"]


def test_claude_install_tolerates_a_utf8_bom():
    """Notepad and friends write a BOM; that is not a reason to refuse or clobber."""
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"permissions": {"allow": ["Bash"]}}', encoding="utf-8-sig")
    install("claude-code")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    assert data["permissions"] == {"allow": ["Bash"]}
    assert HOOK_COMMAND in json.dumps(data["hooks"])


def test_install_keeps_the_existing_file_mode():
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    path.chmod(0o600)
    install("claude-code")
    assert path.stat().st_mode & 0o777 == 0o600


def test_claude_uninstall():
    install("claude-code")
    message = uninstall("claude-code")
    assert "removed" in message
    data = json.loads(claude_settings_path().read_text())
    assert HOOK_COMMAND not in json.dumps(data)


def test_claude_uninstall_keeps_other_hooks():
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "keep-me"}]}]
                }
            }
        )
    )
    install("claude-code")
    uninstall("claude-code")
    assert _hook_commands(path, "UserPromptSubmit") == ["keep-me"]


def test_claude_uninstall_without_settings():
    assert "nothing to remove" in uninstall("claude-code")


# ---------------------------------------------------------------------------
# codex: hooks.json is the default, notify is behind --legacy
# ---------------------------------------------------------------------------


def test_codex_install_writes_hooks_json():
    message = install("codex")
    path = codex_hooks_path()
    assert path.exists()
    document = json.loads(path.read_text())
    assert document["description"] == "agent-prompt-capture"
    for event in CODEX_HOOK_EVENTS:
        assert _hook_commands(path, event) == [CODEX_HOOK_COMMAND]
        assert document["hooks"][event][0]["hooks"][0]["timeout"] == 10
        assert document["hooks"][event][0]["hooks"][0]["type"] == "command"
    assert "UserPromptSubmit" in message and "Stop" in message
    # config.toml now exists, but only to carry the trust entries: no notify line.
    assert "notify" not in codex_config_path().read_text()


def test_codex_install_hooks_is_idempotent():
    install("codex")
    before = codex_hooks_path().read_text()
    message = install("codex")
    assert codex_hooks_path().read_text() == before
    assert "already installed" in message
    for event in CODEX_HOOK_EVENTS:
        assert _hook_commands(codex_hooks_path(), event) == [CODEX_HOOK_COMMAND]


def test_codex_install_preserves_other_hooks():
    path = codex_hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "description": "mine",
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "shell", "hooks": [{"type": "command", "command": "lint"}]}
                    ],
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-tool"}]}],
                },
            }
        )
    )
    install("codex")
    document = json.loads(path.read_text())
    assert document["description"] == "mine"
    assert _hook_commands(path, "PreToolUse") == ["lint"]
    assert _hook_commands(path, "UserPromptSubmit") == ["other-tool", CODEX_HOOK_COMMAND]
    assert _hook_commands(path, "Stop") == [CODEX_HOOK_COMMAND]


def test_codex_install_dry_run():
    message = install("codex", dry_run=True)
    assert "[dry-run]" in message
    assert CODEX_HOOK_COMMAND in message
    assert "would trust 2 codex hook(s)" in message
    assert not codex_hooks_path().exists()
    assert not codex_config_path().exists()


def test_codex_install_honours_codex_home(monkeypatch, tmp_path):
    target = tmp_path / "custom-codex"
    monkeypatch.setenv("CODEX_HOME", str(target))
    install("codex")
    assert (target / "hooks.json").exists()


def test_codex_legacy_install_writes_notify():
    install("codex", legacy=True)
    text = codex_config_path().read_text()
    assert 'notify = ["apc", "capture", "codex"]' in text
    assert not codex_hooks_path().exists()


def test_codex_legacy_appends_to_existing_config():
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('model = "o3"\n\n[tui]\ntheme = "dark"\n')
    install("codex", legacy=True)
    text = path.read_text()
    assert 'model = "o3"' in text
    assert 'notify = ["apc", "capture", "codex"]' in text
    assert '[tui]\ntheme = "dark"' in text
    # the notify line must land before the first section header
    assert text.index("notify") < text.index("[tui]")


def test_codex_legacy_is_idempotent():
    install("codex", legacy=True)
    before = codex_config_path().read_text()
    message = install("codex", legacy=True)
    assert codex_config_path().read_text() == before
    assert "already installed" in message
    assert before.count("notify") == 1


def test_codex_legacy_does_not_clobber_an_existing_notify():
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = 'notify = ["/usr/local/bin/my-notifier"]\nmodel = "o3"\n'
    path.write_text(original)
    message = install("codex", legacy=True)
    assert path.read_text() == original
    assert "already sets notify" in message
    assert 'notify = ["apc", "capture", "codex"]' in message


def test_codex_legacy_ignores_notify_inside_a_section():
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[some.table]\nnotify = ["not-top-level"]\n')
    install("codex", legacy=True)
    text = path.read_text()
    assert 'notify = ["apc", "capture", "codex"]' in text
    assert 'notify = ["not-top-level"]' in text


def test_codex_legacy_dry_run():
    message = install("codex", legacy=True, dry_run=True)
    assert "[dry-run]" in message
    assert not codex_config_path().exists()


def test_codex_uninstall_removes_both():
    install("codex")
    install("codex", legacy=True)
    message = uninstall("codex")
    assert "removed codex hooks" in message
    assert "removed codex notify" in message
    assert CODEX_HOOK_COMMAND not in codex_hooks_path().read_text()
    assert "notify" not in codex_config_path().read_text()


def test_codex_uninstall_keeps_other_hooks_and_config():
    path = codex_hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep"}]}]}})
    )
    config = codex_config_path()
    config.write_text('model = "o3"\n')
    install("codex")
    install("codex", legacy=True)
    uninstall("codex")
    assert _hook_commands(path, "Stop") == ["keep"]
    assert 'model = "o3"' in config.read_text()


def test_codex_uninstall_leaves_foreign_notify():
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('notify = ["someone-else"]\n')
    message = uninstall("codex")
    assert "no apc notify" in message
    assert path.read_text() == 'notify = ["someone-else"]\n'


def test_codex_uninstall_without_files():
    assert "nothing to remove" in uninstall("codex")


# ---------------------------------------------------------------------------
# codex hook trust: Codex ignores a hook until its hash is trusted in config.toml
# ---------------------------------------------------------------------------

#: Confirmed against the real Codex CLI 0.155.1 (2026-09-20) for `apc capture codex`
#: with a timeout of 10. If these change, every hook we ever installed stops firing.
PINNED_HASHES = {
    "user_prompt_submit": (
        "sha256:84bac188cd8cd2224b7d68e5b2bd25390fa243baea19406b97983e6cb3ef61bc"
    ),
    "stop": "sha256:d955e4abf3ea73d405f18346ddf4eb848ed4b574ac58c6635614ba260151f987",
}


def _trust_state(path=None) -> dict:
    data = tomllib.loads((path or codex_config_path()).read_text(encoding="utf-8"))
    return data.get("hooks", {}).get("state", {})


def _our_keys() -> dict[str, str]:
    return {label: codex_hook_state_key(codex_hooks_path(), label) for label in PINNED_HASHES}


def test_codex_trust_hashes_are_pinned():
    for label, digest in PINNED_HASHES.items():
        assert codex_hook_trust_hash(label, CODEX_HOOK_COMMAND, HOOK_TIMEOUT) == digest


def test_codex_trust_hash_changes_with_the_command_and_timeout():
    other = codex_hook_trust_hash("stop", CODEX_HOOK_COMMAND, HOOK_TIMEOUT + 1)
    assert other != PINNED_HASHES["stop"]
    assert codex_hook_trust_hash("stop", "something-else", HOOK_TIMEOUT) != PINNED_HASHES["stop"]


def test_codex_state_key_format(tmp_path):
    key = codex_hook_state_key(tmp_path / "hooks.json", "stop")
    assert key == f"{tmp_path / 'hooks.json'}:stop:0:0"
    assert codex_hook_state_key(tmp_path / "hooks.json", "stop", 1, 2).endswith(":stop:1:2")


def test_codex_install_writes_trust_entries_into_an_empty_config():
    message = install("codex")
    assert f"trusted codex hooks in {codex_config_path()} (2 entries)" in message
    state = _trust_state()
    assert len(state) == 2
    for label, key in _our_keys().items():
        assert state[key]["trusted_hash"] == PINNED_HASHES[label]


def test_codex_install_trust_preserves_unrelated_config_byte_for_byte():
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = 'model = "gpt-5.1-codex"\n\n[tui]\ntheme = "dark"\n'
    path.write_text(original)
    install("codex")
    text = path.read_text()
    assert text.startswith(original)
    appended = text[len(original) :]
    assert "[hooks.state." in appended
    assert len(_trust_state()) == 2


def test_codex_install_trust_keeps_an_existing_hooks_state_table():
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[hooks.state]\n"/elsewhere/hooks.json:stop:0:0" = { trusted_hash = "sha256:other" }\n'
    )
    install("codex")
    state = _trust_state()
    assert state["/elsewhere/hooks.json:stop:0:0"]["trusted_hash"] == "sha256:other"
    for label, key in _our_keys().items():
        assert state[key]["trusted_hash"] == PINNED_HASHES[label]


def test_codex_install_trust_updates_a_stale_inline_hash_in_place():
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = codex_hook_state_key(codex_hooks_path(), "user_prompt_submit")
    path.write_text(f'[hooks.state]\n"{key}" = {{ trusted_hash = "sha256:stale" }}\n')
    install("codex")
    text = path.read_text()
    assert "sha256:stale" not in text
    assert text.count(f'"{key}"') == 1  # updated in place, not duplicated
    assert _trust_state()[key]["trusted_hash"] == PINNED_HASHES["user_prompt_submit"]


def test_codex_install_trust_updates_a_stale_table_header_hash_in_place():
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = codex_hook_state_key(codex_hooks_path(), "stop")
    path.write_text(f'[hooks.state."{key}"]\ntrusted_hash = "sha256:stale"\n')
    install("codex")
    text = path.read_text()
    assert "sha256:stale" not in text
    assert text.count(f'[hooks.state."{key}"]') == 1
    assert _trust_state()[key]["trusted_hash"] == PINNED_HASHES["stop"]


def test_codex_install_trust_is_idempotent_over_three_runs():
    install("codex")
    first = codex_config_path().read_text()
    second_message = install("codex")
    third_message = install("codex")
    assert codex_config_path().read_text() == first
    assert "already trusted" in second_message
    assert second_message == third_message
    assert len(_trust_state()) == 2


def test_codex_install_trust_follows_the_real_group_index():
    path = codex_hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other"}]}]}}
        )
    )
    install("codex")
    state = _trust_state()
    # ours landed in group 1 for UserPromptSubmit, group 0 for Stop
    assert codex_hook_state_key(path, "user_prompt_submit", 1, 0) in state
    assert codex_hook_state_key(path, "stop", 0, 0) in state


def test_codex_install_no_trust_writes_no_entries():
    message = install("codex", trust=False)
    assert not codex_config_path().exists()
    assert "Trust all and continue" in message
    assert len(message.splitlines()) == 2


def test_codex_uninstall_removes_trust_entries_and_leaves_others():
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'model = "gpt-5.1-codex"\n\n[hooks.state]\n'
        '"/elsewhere/hooks.json:stop:0:0" = { trusted_hash = "sha256:other" }\n'
    )
    install("codex")
    message = uninstall("codex")
    assert "removed codex hook trust entries (2)" in message
    state = _trust_state()
    assert list(state) == ["/elsewhere/hooks.json:stop:0:0"]
    text = path.read_text()
    assert 'model = "gpt-5.1-codex"' in text
    for key in _our_keys().values():
        assert key not in text


def test_codex_uninstall_removes_inline_trust_entries():
    install("codex")
    keys = _our_keys()
    path = codex_config_path()
    path.write_text(
        "[hooks.state]\n"
        + "".join(
            f'"{key}" = {{ trusted_hash = "{PINNED_HASHES[label]}" }}\n'
            for label, key in keys.items()
        )
    )
    uninstall("codex")
    assert _trust_state() == {}


def test_codex_uninstall_leaves_a_foreign_trust_entry_for_our_path():
    install("codex")
    key = codex_hook_state_key(codex_hooks_path(), "stop")
    path = codex_config_path()
    path.write_text(f'[hooks.state."{key}"]\ntrusted_hash = "sha256:not-ours"\n')
    message = uninstall("codex")
    assert "no codex hook trust entries" in message
    assert _trust_state()[key]["trusted_hash"] == "sha256:not-ours"


def test_codex_uninstall_survives_a_malformed_config_toml():
    """A config we cannot parse must not stop hooks.json from being cleaned up."""
    install("codex")
    path = codex_config_path()
    path.write_text('model = "unterminated\nnope\n')
    message = uninstall("codex")
    assert "removed codex hooks" in message
    assert "could not read the codex hook trust entries" in message


def test_codex_install_refuses_to_write_when_an_entry_cannot_be_updated():
    """A dotted top-level spelling we cannot edit must not become a duplicate key."""
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = codex_hook_state_key(codex_hooks_path(), "stop")
    original = f'hooks.state."{key}".trusted_hash = "sha256:stale"\n'
    path.write_text(original)
    with pytest.raises(ConfigFormatError, match="refusing to write"):
        install("codex")
    assert path.read_text() == original


def test_codex_install_refuses_a_malformed_config_toml():
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = 'model = "unterminated\nnope\n'
    path.write_text(original)
    with pytest.raises(ConfigFormatError, match="not valid TOML"):
        install("codex")
    assert path.read_text() == original


# ---------------------------------------------------------------------------
# opencode
# ---------------------------------------------------------------------------


def test_opencode_install_copies_the_packaged_plugin():
    message = install("opencode")
    target = opencode_plugin_path()
    assert target.exists()
    assert "apc capture opencode" in target.read_text()
    assert str(target) in message


def test_opencode_install_falls_back_to_the_template(monkeypatch, tmp_path):
    from agent_prompt_capture import installer

    monkeypatch.setattr(installer, "_packaged_plugin", lambda: tmp_path / "missing.js")
    message = install("opencode")
    target = opencode_plugin_path()
    assert target.read_text() == OPENCODE_PLUGIN_SOURCE
    assert "built-in template" in message
    assert "TODO" in target.read_text()
    assert "Bun.spawn" in target.read_text()
    assert "child_process" in target.read_text()


def test_opencode_install_is_idempotent():
    install("opencode")
    first = opencode_plugin_path().read_text()
    install("opencode")
    assert opencode_plugin_path().read_text() == first


def test_opencode_install_honours_xdg_config_home(monkeypatch, tmp_path):
    target = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(target))
    install("opencode")
    assert (target / "opencode" / "plugin" / "agent-prompt-capture.js").exists()


def test_opencode_install_dry_run():
    message = install("opencode", dry_run=True)
    assert "[dry-run]" in message
    assert not opencode_plugin_path().exists()


def test_opencode_uninstall():
    install("opencode")
    assert "removed" in uninstall("opencode")
    assert not opencode_plugin_path().exists()
    assert "nothing to remove" in uninstall("opencode")


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target", ["claude-code", "claude_code", "CLAUDE-CODE", "codex", "opencode"]
)
def test_target_spellings(target):
    assert install(target, dry_run=True)


def test_unknown_target():
    with pytest.raises(ValueError, match="unknown install target"):
        install("emacs")
    with pytest.raises(ValueError, match="unknown uninstall target"):
        uninstall("emacs")


def test_installers_write_nothing_outside_home(tmp_path):
    install("claude-code")
    install("codex")
    install("codex", legacy=True)
    install("opencode")
    home = tmp_path / "home"
    for path in (
        claude_settings_path(),
        codex_config_path(),
        codex_hooks_path(),
        opencode_plugin_path(),
    ):
        assert home in path.parents


# ---------------------------------------------------------------------------
# regressions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["claude-code", "codex", "opencode"])
def test_install_three_times_is_byte_identical(target, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    paths = {
        "claude-code": claude_settings_path,
        "codex": codex_hooks_path,
        "opencode": opencode_plugin_path,
    }
    install(target)
    first = paths[target]().read_bytes()
    install(target)
    install(target)
    assert paths[target]().read_bytes() == first
    if target != "opencode":
        document = json.loads(first)
        blob = json.dumps(document)
        command = HOOK_COMMAND if target == "claude-code" else CODEX_HOOK_COMMAND
        assert blob.count(command) == 2  # exactly one per event, never duplicated


def test_install_leaves_no_temp_files_behind(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    install("claude-code")
    leftovers = list((tmp_path / "claude").glob("*.apc-tmp"))
    assert leftovers == []


def test_a_refused_install_leaves_no_temp_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken")
    with pytest.raises(ValueError):
        install("claude-code")
    assert list(path.parent.glob("*.apc-tmp")) == []


def test_uninstall_is_idempotent_and_leaves_foreign_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "other-tool --go"}]}
                    ],
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}
                    ],
                },
                "permissions": {"allow": ["Bash"]},
            }
        )
    )
    install("claude-code")
    uninstall("claude-code")
    first = path.read_text()
    uninstall("claude-code")
    assert path.read_text() == first
    data = json.loads(first)
    assert HOOK_COMMAND not in json.dumps(data)
    assert "other-tool --go" in json.dumps(data["hooks"]["UserPromptSubmit"])
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert data["permissions"] == {"allow": ["Bash"]}


def test_codex_hooks_honour_codex_home_for_uninstall(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    install("codex")
    assert (tmp_path / "elsewhere" / "hooks.json").exists()
    uninstall("codex")
    assert CODEX_HOOK_COMMAND not in (tmp_path / "elsewhere" / "hooks.json").read_text()


def test_opencode_template_matches_the_packaged_plugin():
    """`apc install opencode` falls back to an embedded copy; it must not drift."""
    import re

    from agent_prompt_capture.installer import OPENCODE_PLUGIN_SOURCE, _packaged_plugin

    packaged = _packaged_plugin()
    assert packaged.is_file(), "the packaged plugin should exist in a source checkout"

    def body(text: str) -> str:
        # Drop the leading comment block; only the code has to agree.
        return re.sub(r"\s+", " ", text[text.index("const COMMAND") :]).strip()

    assert body(OPENCODE_PLUGIN_SOURCE) == body(packaged.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# review regressions (cubic)
# ---------------------------------------------------------------------------


def test_codex_install_normalizes_a_handler_that_has_no_timeout():
    """Codex would hash such a handler with its own 600 s default, so the hook never ran."""
    path = codex_hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    event: [{"hooks": [{"type": "command", "command": CODEX_HOOK_COMMAND}]}]
                    for event in CODEX_HOOK_EVENTS
                }
            }
        )
    )
    install("codex")

    handlers = [
        hook
        for event in CODEX_HOOK_EVENTS
        for matcher in json.loads(path.read_text())["hooks"][event]
        for hook in matcher["hooks"]
    ]
    assert handlers and all(h["timeout"] == HOOK_TIMEOUT for h in handlers)

    # ... and the trust entry hashes exactly what is now on disk.
    state = tomllib.loads(codex_config_path().read_text())["hooks"]["state"]
    key = codex_hook_state_key(path, "stop")
    assert state[key]["trusted_hash"] == codex_hook_trust_hash(
        "stop", CODEX_HOOK_COMMAND, HOOK_TIMEOUT
    )


def test_codex_expected_trust_uses_codex_default_timeout_when_absent():
    """Hash what Codex hashes: a missing timeout normalizes to 600, not to ours."""
    path = codex_hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": CODEX_HOOK_COMMAND}]}]}}
        )
    )
    entries = codex_expected_trust(path)
    assert entries[codex_hook_state_key(path, "stop")] == codex_hook_trust_hash(
        "stop", CODEX_HOOK_COMMAND, CODEX_DEFAULT_TIMEOUT
    )


def test_codex_legacy_leaves_a_foreign_notify_that_merely_starts_with_apc():
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = 'notify = ["apc", "wrap", "--my-own-thing"]\n'
    path.write_text(original)
    message = install("codex", legacy=True)
    assert path.read_text() == original
    assert "already sets notify" in message


def test_codex_uninstall_leaves_a_foreign_notify_that_starts_with_apc():
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = 'notify = ["apc", "wrap", "--my-own-thing"]\n'
    path.write_text(original)
    message = uninstall("codex")
    assert "no apc notify" in message
    assert path.read_text() == original


def test_codex_legacy_still_recognizes_our_own_notify_line():
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("notify = ['apc', 'capture', 'codex']  # installed by apc\n")
    install("codex", legacy=True)
    assert path.read_text().strip() == 'notify = ["apc", "capture", "codex"]'
    assert "no apc notify" not in uninstall("codex")


def test_a_symlinked_temp_path_is_not_followed(tmp_path, monkeypatch):
    """A pre-created `<config>.apc-tmp` symlink must not be written through."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("PRECIOUS\n")
    (path.parent / (path.name + ".apc-tmp")).symlink_to(victim)

    install("claude-code")

    assert victim.read_text() == "PRECIOUS\n"
    assert HOOK_COMMAND in path.read_text()


def test_codex_install_writes_no_hooks_when_the_config_is_malformed():
    """A hooks.json Codex will never be told to trust is worse than no install."""
    config = codex_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('model = "unterminated\nnope\n')
    with pytest.raises(ConfigFormatError, match="not valid TOML"):
        install("codex")
    assert not codex_hooks_path().exists()


def test_codex_install_rolls_hooks_json_back_when_trust_fails():
    config = codex_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    key = codex_hook_state_key(codex_hooks_path(), "stop")
    config.write_text(f'hooks.state."{key}".trusted_hash = "sha256:stale"\n')

    hooks = codex_hooks_path()
    # Nothing registered for Stop, so our Stop hook lands on the stale key above.
    original = json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "keep"}]}]}})
    hooks.write_text(original)

    with pytest.raises(ConfigFormatError, match="refusing to write"):
        install("codex")
    assert hooks.read_text() == original


def test_codex_dry_run_plans_the_normalized_timeout():
    """The dry run must predict the hash the real install will write, not today's."""
    path = codex_hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": CODEX_HOOK_COMMAND}]}]}}
        )
    )
    assert "would trust 2 codex hook(s)" in install("codex", dry_run=True)

    install("codex")
    state = tomllib.loads(codex_config_path().read_text())["hooks"]["state"]
    assert state[codex_hook_state_key(path, "stop")]["trusted_hash"] == codex_hook_trust_hash(
        "stop", CODEX_HOOK_COMMAND, HOOK_TIMEOUT
    )
