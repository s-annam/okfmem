"""Coverage for `okfmem graduate` (issue #10) — the forward complement to
consolidation's decay/archive: promote a durable page into CLAUDE.md/AGENTS.md,
then archive (never delete) the source.

Load-bearing invariants under test:
  (a) symlink-aware AGENTS.md — detect with os.path.islink + readlink, never a
      content compare: symlink-to-CLAUDE.md -> do nothing, real file -> mirror,
      absent -> stays absent.
  (b) archive, never delete — source moves to projects/<proj>/archive/<slug>.md,
      its MEMORY.md pointer is dropped, and its frontmatter is stamped with
      graduated_to so a later curate pass never re-flags/hard-deletes it.
  (c) --dry-run writes nothing (byte-identical tree before/after).
  (d) the write is a rung-2 op: non-interactive without --yes skips and prints
      the manual fallback, and nothing is written.
"""
import os
from types import SimpleNamespace

import memory_graduate as mg

PAGE = (
    "---\n"
    "name: egress-claim\n"
    "description: Egress claim was corrected in the audit\n"
    "type: project\n"
    "---\n\n"
    "# Egress claim correction\n\n"
    "The service does NOT egress to the public internet from the worker pool.\n"
    "**Why:** an earlier doc claimed otherwise; corrected in PR #549.\n"
)

MEMORY_MD = (
    "# MEMORY\n\n"
    "- [Egress claim correction](egress-claim.md) — corrected false claim\n"
    "- [Other page](other.md) — unrelated hook\n"
)


def _store(tmp_path, project="demoproj"):
    store = tmp_path / "store"
    pdir = store / "projects" / project
    pdir.mkdir(parents=True)
    (pdir / "egress-claim.md").write_text(PAGE, encoding="utf-8")
    (pdir / "MEMORY.md").write_text(MEMORY_MD, encoding="utf-8")
    return store


def _repo(tmp_path, with_claude_md=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    if with_claude_md:
        (repo / "CLAUDE.md").write_text(
            "# CLAUDE.md\n\nGuidance for this repo.\n\n"
            "## Hard rules (no exceptions)\n\n- existing rule\n", encoding="utf-8")
    return repo


def _args(*, slug, to, project, store, yes=False, dry_run=False, heading=None,
          pr=None):
    return SimpleNamespace(slug=slug, to=to, heading=heading, project=project,
                           pr=pr, store=store, yes=yes, dry_run=dry_run)


def _snapshot(root):
    snap = {}
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for fn in files:
            p = os.path.join(dirpath, fn)
            with open(p, "rb") as f:
                snap[p] = f.read()
    return snap


# ---------------------------------------------------------------------------
# (a) symlink-aware AGENTS.md
# ---------------------------------------------------------------------------
def test_symlinked_agents_md_is_left_untouched(tmp_path, monkeypatch):
    store = _store(tmp_path)
    repo = _repo(tmp_path)
    agents = repo / "AGENTS.md"
    agents.symlink_to(repo / "CLAUDE.md")
    monkeypatch.setattr(mg, "_current_git_root", lambda: str(repo))

    plan = mg.build_plan(str(store), "egress-claim", "demoproj",
                         str(repo / "CLAUDE.md"), None, None)
    assert plan["agents_action"] == "symlink-noop"
    assert plan["agents_after"] is None

    before_target = os.path.realpath(agents)  # the symlink's resolved target
    mg.apply_plan(plan, str(store), __import__("datetime").date(2026, 7, 23))
    # CLAUDE.md changed (which the symlink already reads through) but the
    # symlink itself was never opened for writing / replaced.
    assert os.path.islink(agents)
    assert os.path.realpath(agents) == before_target


def test_real_agents_md_is_mirrored(tmp_path, monkeypatch):
    store = _store(tmp_path)
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text(
        "# AGENTS.md\n\nGuidance for agents.\n\n"
        "## Hard rules (no exceptions)\n\n- existing rule\n", encoding="utf-8")
    monkeypatch.setattr(mg, "_current_git_root", lambda: str(repo))

    plan = mg.build_plan(str(store), "egress-claim", "demoproj",
                         str(repo / "CLAUDE.md"), None, None)
    assert plan["agents_action"] == "mirror"

    import datetime
    mg.apply_plan(plan, str(store), datetime.date(2026, 7, 23))

    claude_text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    agents_text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "does NOT egress" in claude_text
    assert "does NOT egress" in agents_text


def test_absent_agents_md_stays_absent(tmp_path, monkeypatch):
    store = _store(tmp_path)
    repo = _repo(tmp_path)  # no AGENTS.md at all
    monkeypatch.setattr(mg, "_current_git_root", lambda: str(repo))

    plan = mg.build_plan(str(store), "egress-claim", "demoproj",
                         str(repo / "CLAUDE.md"), None, None)
    assert plan["agents_action"] == "absent"

    import datetime
    mg.apply_plan(plan, str(store), datetime.date(2026, 7, 23))
    assert not (repo / "AGENTS.md").exists()


# ---------------------------------------------------------------------------
# (b) archive, never delete + provenance + MEMORY.md pointer drop
# ---------------------------------------------------------------------------
def test_archive_not_delete_with_provenance_and_pointer_drop(tmp_path, monkeypatch):
    store = _store(tmp_path)
    repo = _repo(tmp_path)
    monkeypatch.setattr(mg, "_current_git_root", lambda: str(repo))

    plan = mg.build_plan(str(store), "egress-claim", "demoproj",
                         str(repo / "CLAUDE.md"), None, "PR#549")
    import datetime
    dest, dropped = mg.apply_plan(plan, str(store), datetime.date(2026, 7, 23))

    src = store / "projects" / "demoproj" / "egress-claim.md"
    archived = store / "projects" / "demoproj" / "archive" / "egress-claim.md"
    assert not src.exists()          # never left behind (moved, not copied)
    assert archived.exists()          # archived, never rm'd
    assert dest == str(archived)

    text = archived.read_text(encoding="utf-8")
    assert "status: archived" in text
    assert "graduated_to: CLAUDE.md#egress-claim-was-corrected-in-the-audit" in text
    assert "PR#549" in text
    assert "2026-07-23" in text

    memory_md = (store / "projects" / "demoproj" / "MEMORY.md").read_text(
        encoding="utf-8")
    assert "egress-claim.md" not in memory_md   # its pointer is gone
    assert "other.md" in memory_md              # unrelated pointer survives
    assert dropped == 1


# ---------------------------------------------------------------------------
# (c) --dry-run writes nothing
# ---------------------------------------------------------------------------
def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    store = _store(tmp_path)
    repo = _repo(tmp_path)
    monkeypatch.setattr(mg, "_current_git_root", lambda: str(repo))

    before_store = _snapshot(str(store))
    before_repo = _snapshot(str(repo))

    args = _args(slug="egress-claim", to=str(repo / "CLAUDE.md"),
                project="demoproj", store=str(store), dry_run=True)
    rc = mg.cmd_graduate(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY-RUN" in out
    assert "does NOT egress" in out  # the diff is shown ...
    assert _snapshot(str(store)) == before_store   # ... but nothing written
    assert _snapshot(str(repo)) == before_repo


# ---------------------------------------------------------------------------
# (d) non-interactive skip without --yes: no write, manual hint printed
# ---------------------------------------------------------------------------
def test_non_interactive_without_yes_skips_and_prints_hint(
        tmp_path, monkeypatch, capsys):
    store = _store(tmp_path)
    repo = _repo(tmp_path)
    monkeypatch.setattr(mg, "_current_git_root", lambda: str(repo))
    monkeypatch.setattr(mg.sys.stdin, "isatty", lambda: False)

    before_store = _snapshot(str(store))
    before_repo = _snapshot(str(repo))

    args = _args(slug="egress-claim", to=str(repo / "CLAUDE.md"),
                project="demoproj", store=str(store), dry_run=False)
    rc = mg.cmd_graduate(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "skipped" in out
    assert "Apply later with: okfmem graduate egress-claim" in out
    assert "--yes" in out
    assert _snapshot(str(store)) == before_store   # declined -> untouched
    assert _snapshot(str(repo)) == before_repo


def test_assume_yes_applies_without_prompting(tmp_path, monkeypatch):
    store = _store(tmp_path)
    repo = _repo(tmp_path)
    monkeypatch.setattr(mg, "_current_git_root", lambda: str(repo))

    args = _args(slug="egress-claim", to=str(repo / "CLAUDE.md"),
                project="demoproj", store=str(store), yes=True, dry_run=False)
    rc = mg.cmd_graduate(args)
    assert rc == 0
    assert "does NOT egress" in (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert not (store / "projects" / "demoproj" / "egress-claim.md").exists()
    assert (store / "projects" / "demoproj" / "archive" / "egress-claim.md").exists()


# ---------------------------------------------------------------------------
# insert_section — heading splice vs. brand-new section
# ---------------------------------------------------------------------------
def test_insert_section_splices_into_existing_heading():
    text = "# CLAUDE.md\n\n## Hard rules (no exceptions)\n\n- rule one\n\n## Next\n\nkept\n"
    out = mg.insert_section(text, "Hard rules (no exceptions)", "- rule two")
    assert "- rule one" in out
    assert "- rule two" in out
    assert out.index("rule two") < out.index("## Next")
    assert "kept" in out


def test_insert_section_appends_new_heading_when_absent():
    text = "# CLAUDE.md\n\nintro\n"
    out = mg.insert_section(text, "New Section", "- fresh rule")
    assert "## New Section" in out
    assert "- fresh rule" in out
    assert out.index("intro") < out.index("## New Section")


# ---------------------------------------------------------------------------
# (B1) path-traversal guard: a crafted <slug> can neither escape the project
# dir nor drive the archive step's os.remove() against an arbitrary file.
# ---------------------------------------------------------------------------
def test_traversal_slug_is_refused_and_nothing_touched(tmp_path, monkeypatch):
    import pytest

    store = _store(tmp_path)
    repo = _repo(tmp_path)
    monkeypatch.setattr(mg, "_current_git_root", lambda: str(repo))

    # An out-of-store sentinel .md the crafted slug tries to reach + delete.
    sentinel = tmp_path / "victim.md"
    sentinel.write_text("do not delete me\n", encoding="utf-8")
    sentinel_before = sentinel.read_bytes()
    before_store = _snapshot(str(store))

    # Full CLI path: refused with rc 2, nothing deleted or written.
    args = _args(slug="../../../../victim", to=str(repo / "CLAUDE.md"),
                 project="demoproj", store=str(store), yes=True)
    rc = mg.cmd_graduate(args)
    assert rc == 2
    assert sentinel.exists() and sentinel.read_bytes() == sentinel_before
    assert _snapshot(str(store)) == before_store   # byte-identical store

    # build_plan raises the module's own error type for the traversal slug.
    with pytest.raises(mg.GraduateError):
        mg.build_plan(str(store), "../../../../victim", "demoproj",
                      str(repo / "CLAUDE.md"), None, None)
    # ...and for a bare `..` component / a separator, before any fs touch.
    for bad in ("..", "sub/egress-claim"):
        with pytest.raises(mg.GraduateError):
            mg.build_plan(str(store), bad, "demoproj",
                          str(repo / "CLAUDE.md"), None, None)


# ---------------------------------------------------------------------------
# (B2) atomicity: a failed CLAUDE.md write rolls the store-internal side back —
# no split-brain (rule in both CLAUDE.md and the page), no page lost.
# ---------------------------------------------------------------------------
def test_claude_write_failure_rolls_back_no_split_brain(tmp_path, monkeypatch):
    import datetime

    import pytest

    store = _store(tmp_path)
    repo = _repo(tmp_path)
    monkeypatch.setattr(mg, "_current_git_root", lambda: str(repo))

    # Make the target's parent dir a FILE so os.makedirs() on it throws when
    # apply_plan reaches the outward CLAUDE.md write — after archiving.
    blocker = repo / "blocker"
    blocker.write_text("i am a file, not a dir\n", encoding="utf-8")
    target = blocker / "CLAUDE.md"

    plan = mg.build_plan(str(store), "egress-claim", "demoproj",
                         str(target), None, None)

    src = store / "projects" / "demoproj" / "egress-claim.md"
    archived = store / "projects" / "demoproj" / "archive" / "egress-claim.md"
    memory_md = store / "projects" / "demoproj" / "MEMORY.md"
    src_before = src.read_bytes()
    memory_before = memory_md.read_bytes()

    with pytest.raises(Exception):
        mg.apply_plan(plan, str(store), datetime.date(2026, 7, 23))

    # Fully rolled back: source restored live and byte-identical, no archived
    # copy, MEMORY.md intact, target never created. The rule lives in exactly
    # one place (the page) — never duplicated, never dropped.
    assert src.exists() and src.read_bytes() == src_before
    assert not archived.exists()
    assert memory_md.read_bytes() == memory_before
    assert not target.exists()


# ---------------------------------------------------------------------------
# (N1) a symlinked AGENTS.md that resolves ELSEWHERE (not the CLAUDE.md being
# edited) is NOT a no-op — the rule must land through it, not silently vanish.
# ---------------------------------------------------------------------------
def test_foreign_symlinked_agents_md_is_mirrored(tmp_path, monkeypatch):
    import datetime

    store = _store(tmp_path)
    repo = _repo(tmp_path)
    other = repo / "other-agents.md"
    other.write_text(
        "# Other\n\n## Hard rules (no exceptions)\n\n- existing rule\n",
        encoding="utf-8")
    (repo / "AGENTS.md").symlink_to(other)   # symlink -> NOT CLAUDE.md
    monkeypatch.setattr(mg, "_current_git_root", lambda: str(repo))

    plan = mg.build_plan(str(store), "egress-claim", "demoproj",
                         str(repo / "CLAUDE.md"), None, None)
    assert plan["agents_action"] == "mirror"   # not "symlink-noop"

    mg.apply_plan(plan, str(store), datetime.date(2026, 7, 23))
    assert "does NOT egress" in (repo / "CLAUDE.md").read_text(encoding="utf-8")
    # the rule reached the foreign symlink's resolved target
    assert "does NOT egress" in other.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (B2-1) a frontmatter-less source page is refused BEFORE any write — a clean
# GraduateError / rc 2, not a bare TypeError mid-apply, and the store untouched.
# ---------------------------------------------------------------------------
def test_frontmatterless_source_refused_before_any_write(tmp_path, monkeypatch):
    import pytest

    store = _store(tmp_path)
    repo = _repo(tmp_path)
    monkeypatch.setattr(mg, "_current_git_root", lambda: str(repo))

    # Overwrite the page with a body that has no leading `---` YAML block.
    pdir = store / "projects" / "demoproj"
    (pdir / "egress-claim.md").write_text(
        "# Egress claim correction\n\nJust a body, no frontmatter at all.\n",
        encoding="utf-8")
    before_store = _snapshot(str(store))

    # build_plan refuses with the module's own error type, before touching disk.
    with pytest.raises(mg.GraduateError):
        mg.build_plan(str(store), "egress-claim", "demoproj",
                      str(repo / "CLAUDE.md"), None, None)

    # Full CLI path: clean rc 2, nothing archived/removed, store byte-identical.
    args = _args(slug="egress-claim", to=str(repo / "CLAUDE.md"),
                 project="demoproj", store=str(store), yes=True)
    rc = mg.cmd_graduate(args)
    assert rc == 2
    assert not (pdir / "archive" / "egress-claim.md").exists()
    assert _snapshot(str(store)) == before_store


# ---------------------------------------------------------------------------
# (B2-2) an os.remove failure INSIDE archive_page (dest written, then remove of
# the live source throws — e.g. a Windows AV/lock) leaves NO split-brain: never
# the live source AND a duplicate archive copy surviving the throw.
# ---------------------------------------------------------------------------
def test_archive_remove_failure_leaves_no_split_brain(tmp_path, monkeypatch):
    import datetime

    import pytest

    store = _store(tmp_path)
    repo = _repo(tmp_path)
    monkeypatch.setattr(mg, "_current_git_root", lambda: str(repo))

    plan = mg.build_plan(str(store), "egress-claim", "demoproj",
                         str(repo / "CLAUDE.md"), None, None)

    src = store / "projects" / "demoproj" / "egress-claim.md"
    archived = store / "projects" / "demoproj" / "archive" / "egress-claim.md"
    memory_md = store / "projects" / "demoproj" / "MEMORY.md"
    src_before = src.read_bytes()
    memory_before = memory_md.read_bytes()

    # archive_page writes dest, then os.remove(src) — make that remove throw
    # only for the source page (a stand-in for a Windows AV/lock), so the
    # archive copy is already on disk when the exception propagates.
    real_remove = os.remove

    def flaky_remove(path, *a, **kw):
        if os.path.abspath(path) == os.path.abspath(str(src)):
            raise OSError("simulated AV/lock on source removal")
        return real_remove(path, *a, **kw)

    monkeypatch.setattr(mg.os, "remove", flaky_remove)
    # archive_page lives in memory_consolidate and calls os.remove there too.
    import memory_consolidate as mc
    monkeypatch.setattr(mc.os, "remove", flaky_remove)

    with pytest.raises(Exception):
        mg.apply_plan(plan, str(store), datetime.date(2026, 7, 23))

    # No split-brain: the live source and a duplicate archive copy must never
    # BOTH survive the throw. Rollback removed the copy (via the still-live real
    # os.remove for the archive path) and left the source intact + byte-equal.
    assert not (src.exists() and archived.exists())
    assert src.exists() and src.read_bytes() == src_before
    assert not archived.exists()
    assert memory_md.read_bytes() == memory_before
    assert not (repo / "CLAUDE.md").read_text(
        encoding="utf-8").count("does NOT egress")
