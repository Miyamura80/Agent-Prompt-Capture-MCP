"""Installer: idempotent merges, preserved keys, atomic writes."""

from __future__ import annotations

import json

import pytest

from agent_prompt_capture.installer import (
    CLAUDE_HOOK_EVENTS,
    CODEX_HOOK_COMMAND,
    CODEX_HOOK_EVENTS,
    HOOK_COMMAND,
    OPENCODE_PLUGIN_SOURCE,
    claude_settings_path,
    codex_config_path,
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
    assert not codex_config_path().exists()


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
    assert not codex_hooks_path().exists()


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
