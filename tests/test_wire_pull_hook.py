import json
import os
import sys

import memory_init as mi


def _expected_command():
    engine = os.path.dirname(os.path.realpath(mi.__file__))
    okfmem_cli = os.path.join(engine, "okfmem")
    return f'"{sys.executable}" "{okfmem_cli}" pull --quiet'


def _patch_home(monkeypatch, tmp_path):
    real_expanduser = mi.os.path.expanduser
    monkeypatch.setattr(
        mi.os.path, "expanduser",
        lambda p: str(tmp_path) if p == "~" else real_expanduser(p))
    return tmp_path


# ---------------------------------------------------------------------------
# wire_pull_hook
# ---------------------------------------------------------------------------

def test_no_claude_dir_returns_no_claude(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    action, path = mi.wire_pull_hook(dry_run=False)
    assert action == "no-claude"
    assert path is None


def test_added_when_no_prior_hook(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()

    action, path = mi.wire_pull_hook(dry_run=False)
    assert action == "added"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    cmds = [h["command"] for g in data["hooks"]["SessionStart"]
            for h in g["hooks"]]
    assert _expected_command() in cmds


def test_dry_run_does_not_write(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"

    action, path = mi.wire_pull_hook(dry_run=True)
    assert action == "added"
    assert not settings.exists()


def test_present_when_already_correctly_wired(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": _expected_command()}]}
        ]}
    }))

    action, path = mi.wire_pull_hook(dry_run=False)
    assert action == "present"


def test_heals_legacy_raw_git_pull_hook(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    legacy_cmd = 'git -C "C:/Users/you/claude-memory" pull --rebase'
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": legacy_cmd}]}
        ]}
    }))

    action, path = mi.wire_pull_hook(dry_run=False)
    assert action == "healed"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    cmds = [h["command"] for g in data["hooks"]["SessionStart"]
            for h in g["hooks"]]
    assert _expected_command() in cmds
    assert legacy_cmd not in cmds


def test_unrelated_git_pull_hook_left_untouched(tmp_path, monkeypatch):
    # An unrelated `git -C <path> pull` SessionStart hook that has nothing to
    # do with the okfmem store must NOT be rewritten by the legacy heal. The
    # old greedy `\bgit\b.*\bpull\b` regex clobbered it; the narrowed one only
    # matches a store-named path.
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    unrelated = 'git -C "/Users/you/notes" pull --rebase'
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": unrelated}]}
        ]}
    }))

    action, path = mi.wire_pull_hook(dry_run=False)
    # Ours is appended; the unrelated hook survives verbatim.
    assert action == "added"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    cmds = [h["command"] for g in data["hooks"]["SessionStart"]
            for h in g["hooks"]]
    assert unrelated in cmds
    assert _expected_command() in cmds


def test_heals_store_path_legacy_line(tmp_path, monkeypatch):
    # The real pre-#16 stale line — `git -C <okfmem-store path> pull` — still
    # heals in place even though the path is the CURRENT store name.
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    legacy_cmd = 'git -C "/Users/you/okfmem-store" pull --rebase'
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": legacy_cmd}]}
        ]}
    }))

    action, path = mi.wire_pull_hook(dry_run=False)
    assert action == "healed"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    cmds = [h["command"] for g in data["hooks"]["SessionStart"]
            for h in g["hooks"]]
    assert _expected_command() in cmds
    assert legacy_cmd not in cmds


def test_is_legacy_pull_command_classification():
    # Store-named raw git pulls are legacy; unrelated ones and okfmem's own
    # managed line are not.
    assert mi._is_legacy_pull_command(
        'git -C "/Users/you/claude-memory" pull --rebase')
    assert mi._is_legacy_pull_command(
        'git -C "/Users/you/okfmem-store" pull')
    assert not mi._is_legacy_pull_command(
        'git -C "/Users/you/notes" pull --rebase')
    assert not mi._is_legacy_pull_command(
        '"/usr/bin/python3" "/Users/you/okfmem/okfmem" pull --quiet')
    # Compound line: store-named fetch + an UNRELATED repo's pull in separate
    # shell segments must NOT be classified legacy (would clobber ~/my-notes).
    assert not mi._is_legacy_pull_command(
        'git -C "/Users/you/okfmem-store" fetch --quiet; '
        'git -C "/Users/you/my-notes" pull --rebase')
    assert not mi._is_legacy_pull_command(
        'git -C "/Users/you/okfmem-store" fetch && git -C "/Users/you/notes" pull')
    # Compound line that is PURELY legacy store ops still heals.
    assert mi._is_legacy_pull_command(
        'git -C "/Users/you/okfmem-store" fetch --quiet; '
        'git -C "/Users/you/okfmem-store" pull --rebase')


def test_heals_stale_okfmem_command_from_older_release(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    stale_cmd = '"/usr/bin/python3" "/old/path/okfmem" pull --quiet'
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": stale_cmd}]}
        ]}
    }))

    action, path = mi.wire_pull_hook(dry_run=False)
    assert action == "healed"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    cmds = [h["command"] for g in data["hooks"]["SessionStart"]
            for h in g["hooks"]]
    assert _expected_command() in cmds
    assert stale_cmd not in cmds


def test_idempotent_second_run_after_heal_is_present(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    legacy_cmd = 'git -C "~/claude-memory" pull --rebase'
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": legacy_cmd}]}
        ]}
    }))

    mi.wire_pull_hook(dry_run=False)
    action, _ = mi.wire_pull_hook(dry_run=False)
    assert action == "present"


def test_skip_on_unreadable_settings(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{not valid json")

    action, _ = mi.wire_pull_hook(dry_run=False)
    assert action.startswith("skip")


def test_preserves_other_session_start_hooks(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    other = {"type": "command", "command": "echo unrelated"}
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [other]}],
                  "Stop": [{"hooks": [{"type": "command",
                                        "command": "echo stop"}]}]}
    }))

    mi.wire_pull_hook(dry_run=False)
    with open(settings, "r", encoding="utf-8") as f:
        data = json.load(f)
    all_cmds = [h["command"] for g in data["hooks"]["SessionStart"]
                for h in g["hooks"]]
    assert "echo unrelated" in all_cmds
    assert _expected_command() in all_cmds
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo stop"


# ---------------------------------------------------------------------------
# detect_legacy_clone
# ---------------------------------------------------------------------------

def test_detect_legacy_clone_found(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    legacy = tmp_path / "claude-memory" / ".git"
    legacy.mkdir(parents=True)

    assert mi.detect_legacy_clone() == str(tmp_path / "claude-memory")


def test_detect_legacy_clone_absent(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    assert mi.detect_legacy_clone() is None


def test_detect_legacy_clone_not_a_git_repo(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    (tmp_path / "claude-memory").mkdir()  # no .git inside

    assert mi.detect_legacy_clone() is None


# ---------------------------------------------------------------------------
# _is_managed_pull_command
# ---------------------------------------------------------------------------

def test_is_managed_pull_command_matches_current_shape():
    assert mi._is_managed_pull_command(
        '"/usr/bin/python3" "/Users/x/okfmem/okfmem" pull --quiet')


def test_is_managed_pull_command_rejects_raw_git():
    assert not mi._is_managed_pull_command(
        'git -C "~/okfmem-store" pull --rebase')
