import json
import os

import memory_init as mi
import memory_uninstall as mu


# ---------------------------------------------------------------------------
# remove_pointer -- inverse of memory_init.upsert_pointer
# ---------------------------------------------------------------------------

def test_remove_pointer_strips_block_and_preserves_surrounding_content(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text("# My notes\n\nSome hand-written content.\n", encoding="utf-8")
    mi.upsert_pointer(str(path), dry_run=False)
    assert mi.MARKER_OPEN in path.read_text(encoding="utf-8")

    action = mu.remove_pointer(str(path), dry_run=False)
    assert action == "removed"

    text = path.read_text(encoding="utf-8")
    assert mi.MARKER_OPEN not in text
    assert mi.MARKER_CLOSE not in text
    assert "# My notes" in text
    assert "Some hand-written content." in text


def test_remove_pointer_preserves_user_blank_line_runs_elsewhere(tmp_path):
    # A user's own multi-blank-line run far from the pointer block must survive
    # verbatim -- remove_pointer normalizes ONLY the seam it created, not the
    # whole file (regression: an earlier version ran a global \n{3,} collapse).
    path = tmp_path / "CLAUDE.md"
    user = "# Top\n\n\n\n# Section with 3 blank lines above\n"
    path.write_text(user, encoding="utf-8")
    mi.upsert_pointer(str(path), dry_run=False)  # appends block at the end

    action = mu.remove_pointer(str(path), dry_run=False)
    assert action == "removed"
    text = path.read_text(encoding="utf-8")
    assert mi.MARKER_OPEN not in text
    assert "\n\n\n\n# Section" in text  # user's blank-line run intact


def test_remove_pointer_second_call_is_idempotent_absent(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text("# My notes\n", encoding="utf-8")
    mi.upsert_pointer(str(path), dry_run=False)

    first = mu.remove_pointer(str(path), dry_run=False)
    assert first == "removed"
    second = mu.remove_pointer(str(path), dry_run=False)
    assert second == "absent"


def test_remove_pointer_file_without_marker_is_untouched(tmp_path):
    path = tmp_path / "CLAUDE.md"
    original = "# My notes\n\nNo pointer here.\n"
    path.write_text(original, encoding="utf-8")

    action = mu.remove_pointer(str(path), dry_run=False)
    assert action == "absent"
    assert path.read_text(encoding="utf-8") == original


def test_remove_pointer_missing_file_reports_no_file(tmp_path):
    path = tmp_path / "does-not-exist.md"
    assert mu.remove_pointer(str(path), dry_run=False) == "no-file"


def test_remove_pointer_dry_run_does_not_write(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text("# My notes\n", encoding="utf-8")
    mi.upsert_pointer(str(path), dry_run=False)
    before = path.read_text(encoding="utf-8")

    action = mu.remove_pointer(str(path), dry_run=True)
    assert action == "removed"
    assert path.read_text(encoding="utf-8") == before  # nothing written


# ---------------------------------------------------------------------------
# _teardown_link -- the shared managed-only removal decision
# ---------------------------------------------------------------------------

def test_teardown_link_removes_managed_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    os.symlink(str(target), str(link), target_is_directory=True)

    action = mu._teardown_link(str(link), dry_run=False)
    assert action == "removed"
    assert not os.path.lexists(str(link))
    assert target.is_dir()  # target itself untouched


def test_teardown_link_skips_real_directory(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "keep.txt").write_text("keep me", encoding="utf-8")

    action = mu._teardown_link(str(real_dir), dry_run=False)
    assert action == "skip (real file)"
    assert real_dir.is_dir()
    assert (real_dir / "keep.txt").read_text(encoding="utf-8") == "keep me"


def test_teardown_link_removes_managed_copy(tmp_path):
    link = tmp_path / "copy"
    link.mkdir()
    (link / mi.MANAGED_COPY_MARKER).write_text("/some/engine/target\n", encoding="utf-8")
    (link / "SKILL.md").write_text("copied content", encoding="utf-8")

    action = mu._teardown_link(str(link), dry_run=False)
    assert action == "removed (copy)"
    assert not os.path.lexists(str(link))


def test_teardown_link_absent_when_nothing_there(tmp_path):
    action = mu._teardown_link(str(tmp_path / "nowhere"), dry_run=False)
    assert action == "absent"


def test_teardown_link_dry_run_does_not_remove(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    os.symlink(str(target), str(link), target_is_directory=True)

    action = mu._teardown_link(str(link), dry_run=True)
    assert action == "removed"
    assert os.path.lexists(str(link))  # dry-run: nothing actually removed


# ---------------------------------------------------------------------------
# unlink_skills -- managed-only removal across a fake harness skill dir
# ---------------------------------------------------------------------------

def test_unlink_skills_removes_managed_link_and_leaves_real_dir(tmp_path, monkeypatch):
    harness_dir = tmp_path / "skills"
    harness_dir.mkdir()
    monkeypatch.setattr(mi, "skill_dirs", lambda: {"fake_harness": str(harness_dir)})

    engine_skills = os.path.join(os.path.dirname(os.path.abspath(mu.__file__)), "skills")
    names = sorted(n for n in os.listdir(engine_skills)
                   if os.path.isfile(os.path.join(engine_skills, n, "SKILL.md")))
    assert len(names) >= 2, "expected at least two packaged skills to exercise this test"
    managed_name, real_name = names[0], names[1]

    # (a) a real link WE made (a symlink), pointing at the real packaged skill.
    os.symlink(os.path.join(engine_skills, managed_name),
               str(harness_dir / managed_name), target_is_directory=True)
    # (b) a plain real directory at another packaged skill's name -- no marker,
    # not a link -- must be treated as the user's own and never removed.
    (harness_dir / real_name).mkdir()
    (harness_dir / real_name / "keep.txt").write_text("keep me", encoding="utf-8")

    actions = mu.unlink_skills(dry_run=False)
    by_name = {name: action for _harness, name, action in actions}

    assert by_name[managed_name] == "removed"
    assert not os.path.lexists(str(harness_dir / managed_name))

    assert by_name[real_name] == "skip (real file)"
    assert (harness_dir / real_name / "keep.txt").read_text(encoding="utf-8") == "keep me"

    # Any packaged skill never linked at all reports absent.
    for name in names:
        if name not in (managed_name, real_name):
            assert by_name[name] == "absent"


# ---------------------------------------------------------------------------
# unlink_project_memory
# ---------------------------------------------------------------------------

def test_unlink_project_memory_removes_registered_link(tmp_path):
    store = tmp_path / "store"
    (store / "projects" / "myproj").mkdir(parents=True)
    claude_projects = tmp_path / "claude_projects"
    root = tmp_path / "repos" / "myproj"
    root.mkdir(parents=True)

    proj_dir = claude_projects / mi.encode_root(str(root))
    proj_dir.mkdir(parents=True)
    link = proj_dir / "memory"
    os.symlink(str(store / "projects" / "myproj"), str(link), target_is_directory=True)

    reg = {"map": {str(root): "myproj"}}
    actions = mu.unlink_project_memory(str(store), str(claude_projects), reg, dry_run=False)
    assert actions == [(str(root), "myproj", "removed")]
    assert not os.path.lexists(str(link))


def test_unlink_project_memory_no_registrations_is_empty(tmp_path):
    store = tmp_path / "store"
    claude_projects = tmp_path / "claude_projects"
    reg = {"map": {}}
    actions = mu.unlink_project_memory(str(store), str(claude_projects), reg, dry_run=False)
    assert actions == []


# ---------------------------------------------------------------------------
# unwire_stop_hook / unwire_pull_hook -- drop ours, preserve everyone else's
# ---------------------------------------------------------------------------

def _patch_home(monkeypatch, tmp_path):
    real_expanduser = mi.os.path.expanduser
    monkeypatch.setattr(
        mi.os.path, "expanduser",
        lambda p: str(tmp_path) if p == "~" else real_expanduser(p))
    monkeypatch.setattr(
        mu.os.path, "expanduser",
        lambda p: str(tmp_path) if p == "~" else real_expanduser(p))
    return tmp_path


def test_unwire_stop_hook_drops_ours_keeps_unrelated_hook(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    ours = {"type": "command", "command": '"/usr/bin/python3" "/e/memory_consolidate.py" --stdin-hook'}
    theirs = {"type": "command", "command": "echo unrelated user hook"}
    settings.write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [ours, theirs]}]}
    }), encoding="utf-8")

    action, path = mu.unwire_stop_hook(dry_run=False)
    assert action == "removed"

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    cmds = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert ours["command"] not in cmds
    assert theirs["command"] in cmds


def test_unwire_stop_hook_prunes_emptied_group(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    ours = {"type": "command", "command": '"/usr/bin/python3" "/e/memory_consolidate.py" --stdin-hook'}
    settings.write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [ours]}]}
    }), encoding="utf-8")

    action, path = mu.unwire_stop_hook(dry_run=False)
    assert action == "removed"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["hooks"]["Stop"] == []


def test_unwire_stop_hook_idempotent_second_run_absent(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    mi.wire_stop_hook(dry_run=False)

    first = mu.unwire_stop_hook(dry_run=False)
    assert first[0] == "removed"
    second = mu.unwire_stop_hook(dry_run=False)
    assert second[0] == "absent"


def test_unwire_stop_hook_no_claude_dir(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    action, path = mu.unwire_stop_hook(dry_run=False)
    assert action == "no-claude"
    assert path is None


def test_unwire_stop_hook_dry_run_does_not_write(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    mi.wire_stop_hook(dry_run=False)
    before = (claude_dir / "settings.json").read_text(encoding="utf-8")

    action, _ = mu.unwire_stop_hook(dry_run=True)
    assert action == "removed"
    assert (claude_dir / "settings.json").read_text(encoding="utf-8") == before


def test_unwire_pull_hook_drops_managed_keeps_legacy_and_unrelated(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    mi.wire_pull_hook(dry_run=False)  # writes our managed command
    with open(settings, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Add an unrelated user hook and a legacy (never-healed) raw-git hook the
    # uninstaller must NOT touch.
    legacy = {"type": "command", "command": 'git -C "~/notes" pull --rebase'}
    other = {"type": "command", "command": "echo unrelated"}
    data["hooks"]["SessionStart"][0]["hooks"].append(legacy)
    data["hooks"]["SessionStart"].append({"hooks": [other]})
    settings.write_text(json.dumps(data), encoding="utf-8")

    action, path = mu.unwire_pull_hook(dry_run=False)
    assert action == "removed"

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    cmds = [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]
    assert legacy["command"] in cmds  # never-healed legacy hook left alone
    assert other["command"] in cmds   # unrelated user hook left alone
    assert not any(mi._is_managed_pull_command(c) for c in cmds)


def test_unwire_pull_hook_no_claude_dir(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    action, path = mu.unwire_pull_hook(dry_run=False)
    assert action == "no-claude"
    assert path is None


def test_unwire_pull_hook_absent_when_settings_missing(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    (tmp_path / ".claude").mkdir()
    action, _ = mu.unwire_pull_hook(dry_run=False)
    assert action == "absent"


def test_unwire_hooks_skip_on_unreadable_settings(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{not valid json", encoding="utf-8")

    action, _ = mu.unwire_stop_hook(dry_run=False)
    assert action.startswith("skip")
    action, _ = mu.unwire_pull_hook(dry_run=False)
    assert action.startswith("skip")
