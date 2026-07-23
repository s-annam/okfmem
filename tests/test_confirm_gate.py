"""Coverage for the rung-2 confirmation gate (_prompt_yes_no) that guards
memory_init's config-mutating ops (settings.json hooks + ~/.claude links).

The invariant that matters: a non-interactive run WITHOUT --yes never blocks --
it takes the safe default (skip) and prints the manual fallback -- while --yes
(installers/CI) applies without prompting.
"""
import builtins
import io
import os
import contextlib

import memory_init as mi

HINT = "Apply later with: okfmem init --yes"


def _gate(**kw):
    kw.setdefault("assume_yes", False)
    kw.setdefault("non_interactive", False)
    kw.setdefault("manual_hint", HINT)
    return mi._prompt_yes_no("Wire hooks + links?", **kw)


def test_assume_yes_short_circuits_true(monkeypatch, capsys):
    # --yes must NOT prompt (input() would raise here if called).
    monkeypatch.setattr(builtins, "input",
                        lambda *a: (_ for _ in ()).throw(AssertionError("prompted")))
    assert _gate(assume_yes=True) is True


def test_non_interactive_without_yes_skips_and_prints_hint(capsys):
    assert _gate(non_interactive=True) is False
    out = capsys.readouterr().out
    assert "skipped" in out
    assert HINT in out  # manual fallback printed so the user can apply later


def test_interactive_yes_proceeds(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda *a: "y")
    assert _gate() is True
    monkeypatch.setattr(builtins, "input", lambda *a: "YES")
    assert _gate() is True


def test_interactive_no_or_default_skips(monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda *a: "n")
    assert _gate() is False
    monkeypatch.setattr(builtins, "input", lambda *a: "")  # bare Enter -> No
    assert _gate() is False
    assert HINT in capsys.readouterr().out


def test_eof_mid_prompt_is_safe_default(monkeypatch, capsys):
    def _eof(*a):
        raise EOFError
    monkeypatch.setattr(builtins, "input", _eof)
    assert _gate() is False
    assert HINT in capsys.readouterr().out


# --------------------------------------------------------------------------
# cmd_run integration: declining the gate must NOT mutate user config
# (regression for the pointer-write leak: step 3 upsert_pointer was ungated)
# --------------------------------------------------------------------------

def _isolated_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("# my hand-edited global\n",
                                                encoding="utf-8")
    store = tmp_path / "store"
    (store / "projects" / "okfmem").mkdir(parents=True)
    real = os.path.expanduser
    monkeypatch.setattr(
        mi.os.path, "expanduser",
        lambda p: (str(home) + p[1:]) if p == "~" or p[:2] in ("~/", "~\\")
        else real(p))
    return home, store


def _run(store):
    with contextlib.redirect_stdout(io.StringIO()):
        mi.cmd_run(str(store), dry_run=False, apply_cleanup=False,
                   wire_hook=True, assume_yes=False)


def test_decline_does_not_write_pointer_block_or_settings(tmp_path, monkeypatch):
    home, store = _isolated_env(tmp_path, monkeypatch)
    # Force the non-interactive decline path (no TTY -> skip, no prompt).
    monkeypatch.setattr(mi.sys.stdin, "isatty", lambda: False)

    _run(store)

    claude_md = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert mi.MARKER_OPEN not in claude_md  # global left untouched on decline
    assert not (home / ".claude" / "settings.json").exists()


def test_assume_yes_writes_pointer_block(tmp_path, monkeypatch):
    home, store = _isolated_env(tmp_path, monkeypatch)
    with contextlib.redirect_stdout(io.StringIO()):
        mi.cmd_run(str(store), dry_run=False, apply_cleanup=False,
                   wire_hook=True, assume_yes=True)
    claude_md = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert mi.MARKER_OPEN in claude_md  # consented -> pointer injected
