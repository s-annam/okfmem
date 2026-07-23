"""Coverage for the statusline save-state badge + session breadcrumb
(memory_consolidate.py): classification logic, the hardened badge writer, and
the git-ignored breadcrumb.
"""
import os
from datetime import date

import memory_consolidate as mc


# ---------------------------------------------------------------------------
# compute_save_state — position-based (last work vs last save)
# ---------------------------------------------------------------------------
def _edit(fp="/x/y.py"):
    return f'{{"type":"tool_use","name":"Edit","input":{{"file_path":"{fp}"}}}}'


def test_none_when_no_transcript():
    assert mc.compute_save_state(None) is None
    assert mc.compute_save_state("") is None


def test_none_when_no_work():
    # A read-only chat session — nothing to capture, no badge.
    t = '{"type":"text","text":"just talking"}\n{"name":"Read"}'
    assert mc.compute_save_state(t) is None


def test_unsaved_when_work_and_no_save():
    assert mc.compute_save_state(_edit()) == "unsaved"


def test_unsaved_on_git_commit_even_without_edit_tool():
    t = '{"name":"Bash","input":{"command":"git commit -m wip"}}'
    assert mc.compute_save_state(t) == "unsaved"


def test_saved_when_save_follows_work():
    t = _edit() + '\n{"text":"running /okfmem-save now"}'
    assert mc.compute_save_state(t) == "saved"


def test_reedit_after_save_flips_back_to_unsaved():
    # save, then more work -> the last work is newer than the last save.
    t = "\n".join([_edit(), "okfmem sync", _edit("/x/z.py")])
    assert mc.compute_save_state(t) == "unsaved"


# ---------------------------------------------------------------------------
# write_status_badge — hardened, best-effort
# ---------------------------------------------------------------------------
def test_badge_write_and_clear(tmp_path, monkeypatch):
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    flag = cfg / ".okfmem-status"

    mc.write_status_badge("unsaved")
    assert flag.read_text().strip() == "unsaved"

    mc.write_status_badge(None)  # clear
    assert not flag.exists()


def test_badge_no_config_dir_is_silent_noop(tmp_path, monkeypatch):
    # Point at a config dir that doesn't exist — no Claude Code, nothing to badge.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    mc.write_status_badge("unsaved")  # must not raise
    assert not (tmp_path / "nope" / ".okfmem-status").exists()


def test_badge_refuses_symlink_target(tmp_path, monkeypatch):
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    secret = tmp_path / "secret"
    secret.write_text("private")
    flag = cfg / ".okfmem-status"
    os.symlink(secret, flag)

    mc.write_status_badge("saved")

    # The symlink (and its target) are left untouched — never written through.
    assert secret.read_text() == "private"
    assert os.path.islink(flag)


# ---------------------------------------------------------------------------
# write_breadcrumb — git-ignored single-session trail
# ---------------------------------------------------------------------------
def test_breadcrumb_records_cwd_and_touched_files(tmp_path):
    store = str(tmp_path / "store")
    os.makedirs(store)
    t = _edit("/repo/a.py") + "\n" + _edit("/repo/b.py")

    mc.write_breadcrumb(store, "/repo", t, date(2026, 7, 23))

    trail = tmp_path / "store" / ".session-trail.md"
    text = trail.read_text()
    assert "cwd: /repo" in text
    assert "/repo/a.py" in text
    assert "/repo/b.py" in text


def test_breadcrumb_is_ignored_by_managed_gitignore():
    import memory_init as mi
    assert ".session-trail.md" in mi.GITIGNORE_LINES
