"""Coverage for the statusline save-state badge + session breadcrumb
(memory_consolidate.py): classification logic, the hardened badge writer, and
the git-ignored breadcrumb.
"""
import json
import os
import tempfile
from datetime import date

import memory_consolidate as mc


# ---------------------------------------------------------------------------
# compute_save_state — position-based (last work vs last save)
# ---------------------------------------------------------------------------
def _edit(fp="/x/y.py"):
    return f'{{"type":"tool_use","name":"Edit","input":{{"file_path":"{fp}"}}}}'


def _write(fp):
    return json.dumps({"type": "tool_use", "name": "Write",
                       "input": {"file_path": fp}})


def _bash(command):
    return json.dumps({"type": "tool_use", "name": "Bash",
                       "input": {"command": command}})


def _save_skill():
    return json.dumps({"type": "tool_use", "name": "Skill",
                       "input": {"skill": "okfmem-save"}})


def _user(text):
    """A user-authored message, in the real Claude Code envelope shape."""
    return json.dumps({"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": text}]}})


def _tool_result(text):
    """Tool OUTPUT — carried in a user-role message, but not user-authored."""
    return json.dumps({"type": "user", "message": {
        "role": "user", "content": [{"type": "tool_result", "content": text}]}})


def _assistant(text):
    return json.dumps({"type": "assistant", "message": {
        "role": "assistant", "content": [{"type": "text", "text": text}]}})


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
    assert mc.compute_save_state(_bash("git commit -m wip")) == "unsaved"


def test_saved_when_save_skill_follows_work():
    assert mc.compute_save_state(_edit() + "\n" + _save_skill()) == "saved"


def test_saved_when_user_invokes_slash_command_after_work():
    assert mc.compute_save_state(_edit() + "\n" + _user("/okfmem-save")) == "saved"
    # ...and via the `<command-name>` envelope, plus the /primer alias.
    t = _edit() + "\n" + _user("<command-name>/primer</command-name>")
    assert mc.compute_save_state(t) == "saved"


def test_reedit_after_save_flips_back_to_unsaved():
    # save, then more work -> the last work is newer than the last save.
    t = "\n".join([_edit(), _bash("okfmem sync"), _edit("/x/z.py")])
    assert mc.compute_save_state(t) == "unsaved"


def test_work_and_save_in_same_record_ties_to_unsaved():
    # Tie breaks toward nagging: a redundant save is cheap, a lost one is not.
    t = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/y.py"}},
        {"type": "tool_use", "name": "Skill", "input": {"skill": "okfmem-save"}},
    ]}})
    assert mc.compute_save_state(t) == "unsaved"


# ---------------------------------------------------------------------------
# scratch writes are not durable work — false-'unsaved' fix (#49)
# ---------------------------------------------------------------------------
def test_scratch_only_session_is_cleared_not_unsaved():
    # A session whose only writes land in the scratchpad / temp roots left
    # nothing to capture (e.g. issue-body drafts): badge cleared, not amber.
    # The real Claude scratchpad lives UNDER a temp root, so it is caught by the
    # ephemeral-root prefix check — not by any blanket 'scratchpad'-segment rule.
    t = "\n".join([
        _write("/tmp/issue-body-49.md"),
        _write("/private/tmp/claude-501/enc/session/scratchpad/draft.md"),
    ])
    assert mc.compute_save_state(t) is None


def test_project_internal_scratchpad_is_unsaved_not_cleared():
    # A committed docs/scratchpad/ (or any project-internal 'scratchpad/' dir)
    # is NOT ephemeral: a durable edit there must nag 'unsaved', never be swept
    # in and silently lost. Regression guard for the over-broad segment match.
    t = _write(os.path.join("/home/whoever/proj", "scratchpad", "note.md"))
    assert mc.is_scratch_path("/home/whoever/proj/scratchpad/note.md") is False
    assert mc.compute_save_state(t) == "unsaved"


def test_scratch_plus_real_write_is_unsaved():
    # One real repo write among scratch writes still nags — bias preserved.
    t = "\n".join([
        _write("/tmp/scratch.md"),
        _write("/repo/src/real.py"),
    ])
    assert mc.compute_save_state(t) == "unsaved"


def test_scratch_plus_git_commit_is_unsaved():
    # commit detection is untouched: committing is always real work regardless
    # of any file path, even if every Write this session was to scratch.
    t = "\n".join([
        _write("/tmp/scratch.md"),
        _bash("git commit -m wip"),
    ])
    assert mc.compute_save_state(t) == "unsaved"


def test_save_after_real_write_still_saved_with_scratch_present():
    t = "\n".join([
        _write("/tmp/scratch.md"),
        _edit("/repo/a.py"),
        _bash("okfmem sync"),
    ])
    assert mc.compute_save_state(t) == "saved"


def test_tmpdir_is_respected_via_gettempdir():
    # macOS $TMPDIR lives under /var/folders/..., not /tmp — gettempdir() must
    # catch it. Build a path under the actual tempdir so this holds anywhere.
    fp = os.path.join(tempfile.gettempdir(), "okfmem-scratch-49.md")
    assert mc.is_scratch_path(fp) is True
    assert mc.compute_save_state(_write(fp)) is None


def test_is_scratch_path_handles_malformed_input():
    # No crash on None / empty / non-string tool input.
    assert mc.is_scratch_path(None) is False
    assert mc.is_scratch_path("") is False
    assert mc.is_scratch_path(123) is False
    assert mc.is_scratch_path(["/tmp/x"]) is False


def test_is_scratch_path_true_and_false_cases():
    assert mc.is_scratch_path("/tmp/x.md") is True
    assert mc.is_scratch_path("/private/tmp/y.md") is True
    # The real per-session scratchpad sits UNDER a temp root — caught by prefix.
    assert mc.is_scratch_path("/private/tmp/claude-501/enc/scratchpad/c.md") is True
    # A real repo path is not scratch; nor is a path merely NAMED like one; nor
    # is a project-internal scratchpad/ dir nested arbitrarily deep in the tree.
    assert mc.is_scratch_path("/repo/src/main.py") is False
    assert mc.is_scratch_path("/repo/tmpfile.py") is False
    assert mc.is_scratch_path("/repo/scratchpad.md") is False
    assert mc.is_scratch_path("/repo/docs/scratchpad/architecture-decision.md") is False


def test_touched_files_omits_scratch_paths():
    # Temp-root writes are omitted; a project-internal scratchpad/ path is a
    # durable edit and is RETAINED (not swept in by a blanket segment match).
    t = "\n".join([
        _write("/repo/real.py"),
        _write("/tmp/draft.md"),
        _write("/private/tmp/claude-501/enc/scratchpad/note.md"),
        _write("/repo/docs/scratchpad/adr.md"),
    ])
    assert sorted(mc.touched_files(t)) == sorted(
        ["/repo/real.py", "/repo/docs/scratchpad/adr.md"]
    )


# ---------------------------------------------------------------------------
# structure, not prose — regressions for the flat-text matcher this replaced
# ---------------------------------------------------------------------------
def test_prose_mentioning_work_is_not_work():
    # Discussing a commit, or quoting the tool-use JSON, is not doing either.
    t = "\n".join([
        _assistant("you should run git commit when done"),
        _user('what does {"name": "Edit"} mean?'),
        _tool_result("CLAUDE.md says: some repos hard-block `git commit`"),
    ])
    assert mc.compute_save_state(t) is None


def test_prose_mentioning_save_does_not_mark_saved():
    # THE dangerous direction: real work, then the words "/okfmem-save" appear
    # in assistant prose / a read file. The badge must still nag.
    t = "\n".join([
        _edit(),
        _assistant("Want me to run /okfmem-save before clearing?"),
        _tool_result("# reminder: remind them to run /okfmem-save"),
    ])
    assert mc.compute_save_state(t) == "unsaved"


def test_reading_a_file_containing_tool_use_json_is_not_work():
    # A tool_result carrying this module's own source must not self-trigger.
    t = _tool_result('WORK_RE = re.compile(r\'"name":\\s*"(?:Edit|Write)"\')')
    assert mc.compute_save_state(t) is None


def test_command_must_run_not_merely_mention():
    assert mc.compute_save_state(_bash("grep -o 'git commit' log.txt")) is None
    # A quoted separator must not leave the tail in apparent command position.
    t = _edit() + "\n" + _bash(r"grep -o 'okfmem[- ]save\|okfmem sync' f")
    assert mc.compute_save_state(t) == "unsaved"


def test_save_is_recognised_through_path_and_interpreter_prefixes():
    # `okfmem` is an extensionless Python script, so the real close-out is
    # `python3 ~/okfmem/okfmem sync` — matching only the bare spelling left the
    # badge amber over every genuinely saved session.
    for command in ("okfmem sync",
                    "~/okfmem/okfmem sync",
                    "/abs/path/okfmem sync",
                    "./okfmem sync",
                    'python3 ~/okfmem/okfmem sync -m "msg"',
                    "okfmem.cmd sync",
                    "pwsh -File ./okfmem.ps1 sync",
                    "cd /r && python3 ~/okfmem/okfmem sync"):
        t = _edit() + "\n" + _bash(command)
        assert mc.compute_save_state(t) == "saved", command


def test_interpreter_stripping_does_not_manufacture_a_save():
    # What follows the interpreter must still be the program: the same words
    # as plain arguments are not a save, nor is merely naming the command.
    for command in ("py build.py --then okfmem sync",
                    "grep -o 'okfmem sync' f",
                    'echo "run okfmem sync"'):
        t = _edit() + "\n" + _bash(command)
        assert mc.compute_save_state(t) == "unsaved", command


def test_git_commit_variants_still_count_as_work():
    for command in ("git commit -m x", "git -C /repo commit --amend",
                    "cd /r && FOO=1 git commit", "sudo git commit -m x"):
        assert mc.compute_save_state(_bash(command)) == "unsaved", command


def test_malformed_lines_are_skipped_not_fatal():
    t = "\n".join(["not json at all", "{broken", "", _edit()])
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


def test_breadcrumb_lists_only_files_actually_written(tmp_path):
    # A path merely quoted in prose or read (not written) is not "touched".
    store = str(tmp_path / "store")
    os.makedirs(store)
    t = "\n".join([
        _edit("/repo/written.py"),
        _tool_result('{"file_path": "/repo/only_quoted.py"}'),
        json.dumps({"type": "tool_use", "name": "Read",
                    "input": {"file_path": "/repo/only_read.py"}}),
    ])

    mc.write_breadcrumb(store, "/repo", t, date(2026, 7, 24))

    text = (tmp_path / "store" / ".session-trail.md").read_text()
    assert "/repo/written.py" in text
    assert "/repo/only_quoted.py" not in text
    assert "/repo/only_read.py" not in text


def test_breadcrumb_is_ignored_by_managed_gitignore():
    import memory_init as mi
    assert ".session-trail.md" in mi.GITIGNORE_LINES
