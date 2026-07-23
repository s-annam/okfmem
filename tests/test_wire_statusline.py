"""Coverage for the opt-in statusline badge wiring (memory_init.wire_statusline):
set the badge only when no statusline exists, never clobber a custom one, and be
idempotent on re-run.
"""
import json
import os

import memory_init as mi


def _settings(tmp_path, data):
    claude = tmp_path / ".claude"
    claude.mkdir(exist_ok=True)
    p = claude / "settings.json"
    p.write_text(json.dumps(data))
    return p


def test_added_when_no_statusline(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    p = _settings(tmp_path, {"hooks": {}})

    action, path = mi.wire_statusline(dry_run=False)

    assert action == "added"
    data = json.loads(p.read_text())
    assert "okfmem-statusline" in data["statusLine"]["command"]


def test_present_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    _settings(tmp_path, {"hooks": {}})

    first, _ = mi.wire_statusline(dry_run=False)
    assert first == "added"
    second, _ = mi.wire_statusline(dry_run=False)
    assert second == "present"


def test_custom_statusline_never_clobbered(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    custom = {"type": "command", "command": "bash ~/.claude/mystatus.sh"}
    p = _settings(tmp_path, {"statusLine": custom})

    action, _ = mi.wire_statusline(dry_run=False)

    assert action == "custom"
    assert json.loads(p.read_text())["statusLine"] == custom


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    p = _settings(tmp_path, {"hooks": {}})

    action, _ = mi.wire_statusline(dry_run=True)

    assert action == "added"
    assert "statusLine" not in json.loads(p.read_text())


def test_no_claude_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    # no .claude created
    action, path = mi.wire_statusline(dry_run=False)
    assert action == "no-claude"
    assert path is None


def test_unreadable_settings_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text("{ not json")

    action, _ = mi.wire_statusline(dry_run=False)

    assert action.startswith("skip")


def test_statusline_state_probe_is_readonly(tmp_path, monkeypatch):
    """The installers branch their prompt copy on statusline_state(); it must
    map wire actions to the one-word token AND write nothing."""
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))

    # no ~/.claude
    assert mi.statusline_state() == "no-claude"

    # ~/.claude, no statusLine -> 'none' (a real wire WOULD add it)
    p = _settings(tmp_path, {"hooks": {}})
    assert mi.statusline_state() == "none"
    assert "statusLine" not in json.loads(p.read_text())  # probe wrote nothing

    # custom statusLine -> 'custom', untouched
    custom = {"type": "command", "command": "bash ~/.claude/mystatus.sh"}
    p.write_text(json.dumps({"statusLine": custom}))
    assert mi.statusline_state() == "custom"
    assert json.loads(p.read_text())["statusLine"] == custom

    # okfmem badge already wired -> 'okfmem'
    mi.wire_statusline(dry_run=False)  # from the no-statusLine state? overwrite:
    p.write_text(json.dumps({"hooks": {}}))
    mi.wire_statusline(dry_run=False)
    assert mi.statusline_state() == "okfmem"

    # unreadable -> 'skip'
    p.write_text("{ not json")
    assert mi.statusline_state() == "skip"
