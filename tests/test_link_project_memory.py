import os
import sys

import pytest

import memory_init as mi


# ---------------------------------------------------------------------------
# encode_root — the inverse of decode_root
# ---------------------------------------------------------------------------

def test_encode_root_matches_decode_root_round_trip(tmp_path):
    project = tmp_path / "my-cool-project"
    project.mkdir()
    encoded = mi.encode_root(str(project))
    if os.name != "nt":
        # POSIX: only '/' is meaningful, so encode is a plain separator swap.
        # On Windows the drive colon is also encoded ('C:\\' -> 'C--'), so this
        # naive replacement doesn't hold -- the round-trip below is the invariant.
        assert encoded == str(project).replace(os.sep, "-")
    assert mi.decode_root(encoded) == os.path.normpath(str(project))


@pytest.mark.skipif(sys.platform == "win32",
                     reason="asserts the POSIX-only replacement rule")
def test_encode_root_posix_leaves_colon_alone():
    # No drive letters on POSIX -- only '/' is meaningful to replace.
    assert mi.encode_root("/Users/you/okfmem") == "-Users-you-okfmem"


def test_encode_root_windows_double_dashes_the_drive_colon(monkeypatch):
    monkeypatch.setattr(mi.os, "name", "nt")
    # normpath under a non-Windows Python won't rewrite '/' to '\\', so build
    # the path with the separator explicitly rather than relying on normpath.
    encoded = mi.encode_root("C:\\Users\\you\\okfmem")
    assert encoded == "C--Users-you-okfmem"
    # And it must round-trip through decode_root's drive-letter probe.
    tokens = encoded.lstrip("-").split("-")
    real_isdir = mi.os.path.isdir
    monkeypatch.setattr(mi.os.path, "isdir",
                         lambda p: True if p == "C:\\" else real_isdir(p))
    assert mi._windows_drive_root(tokens) == ("C:\\", 1)


# ---------------------------------------------------------------------------
# link_project_memory
# ---------------------------------------------------------------------------

HARNESS = object()  # sentinel path value; only truthiness is checked


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake store + fake ~/.claude/projects layout, with _current_git_root
    stubbed to a fixed root so tests don't need a real git repo."""
    store = tmp_path / "store"
    (store / "projects" / "myproj").mkdir(parents=True)
    claude_projects = tmp_path / "claude_projects"
    claude_projects.mkdir()
    root = tmp_path / "repos" / "myproj"
    root.mkdir(parents=True)
    monkeypatch.setattr(mi, "_current_git_root", lambda: str(root))
    harnesses = {"claude_code": "/fake/.claude/CLAUDE.md"}
    reg = {"overrides": {}}
    return {
        "store": str(store),
        "claude_projects": str(claude_projects),
        "harnesses": harnesses,
        "reg": reg,
        "root": root,
    }


def _link_path(env):
    encoded = mi.encode_root(str(env["root"]))
    return os.path.join(env["claude_projects"], encoded, "memory")


def test_creates_symlink_when_missing(env):
    status, msg = mi.link_project_memory(
        env["store"], env["claude_projects"], env["harnesses"], env["reg"],
        dry_run=False)
    assert status == "changed"
    assert "linked to myproj" in msg
    link = _link_path(env)
    assert os.path.islink(link)
    assert os.path.realpath(link) == os.path.realpath(
        os.path.join(env["store"], "projects", "myproj"))


def test_idempotent_second_run_is_ok(env):
    mi.link_project_memory(env["store"], env["claude_projects"],
                            env["harnesses"], env["reg"], dry_run=False)
    status, msg = mi.link_project_memory(
        env["store"], env["claude_projects"], env["harnesses"], env["reg"],
        dry_run=False)
    assert status == "ok"
    assert "myproj" in msg


def test_dry_run_reports_without_creating(env):
    status, msg = mi.link_project_memory(
        env["store"], env["claude_projects"], env["harnesses"], env["reg"],
        dry_run=True)
    assert status == "changed"
    assert "would link" in msg
    assert not os.path.exists(_link_path(env))


def test_skip_when_no_claude_code_harness(env):
    env["harnesses"] = {"claude_code": None}
    status, msg = mi.link_project_memory(
        env["store"], env["claude_projects"], env["harnesses"], env["reg"],
        dry_run=False)
    assert status == "skip"
    assert "harness" in msg


def test_skip_when_not_a_git_repo(env, monkeypatch):
    monkeypatch.setattr(mi, "_current_git_root", lambda: None)
    status, msg = mi.link_project_memory(
        env["store"], env["claude_projects"], env["harnesses"], env["reg"],
        dry_run=False)
    assert status == "skip"
    assert "git repo" in msg


def test_skip_when_store_has_no_project_dir_yet(env, monkeypatch):
    # A repo whose derived name ("unlinked") has no store/projects dir yet --
    # e.g. the very first `init` before any page has ever been authored.
    other_root = env["root"].parent / "unlinked"
    other_root.mkdir()
    monkeypatch.setattr(mi, "_current_git_root", lambda: str(other_root))

    status, msg = mi.link_project_memory(
        env["store"], env["claude_projects"], env["harnesses"],
        {"overrides": {}}, dry_run=False)
    assert status == "skip"
    assert "no store project dir" in msg


def test_repoints_a_broken_symlink(env):
    link = _link_path(env)
    os.makedirs(os.path.dirname(link))
    wrong_target = env["root"].parent  # anything other than the real target
    os.symlink(str(wrong_target), link, target_is_directory=True)

    status, msg = mi.link_project_memory(
        env["store"], env["claude_projects"], env["harnesses"], env["reg"],
        dry_run=False)
    assert status == "changed"
    assert "repointed" in msg
    assert os.path.realpath(link) == os.path.realpath(
        os.path.join(env["store"], "projects", "myproj"))


def test_replaces_empty_placeholder_directory(env):
    # The exact bug this issue fixes: a fresh machine leaves an empty real
    # directory instead of a symlink.
    link = _link_path(env)
    os.makedirs(link)

    status, msg = mi.link_project_memory(
        env["store"], env["claude_projects"], env["harnesses"], env["reg"],
        dry_run=False)
    assert status == "changed"
    assert os.path.islink(link)


def test_skips_nonempty_real_directory(env):
    link = _link_path(env)
    os.makedirs(link)
    with open(os.path.join(link, "some-real-page.md"), "w") as f:
        f.write("do not eat me")

    status, msg = mi.link_project_memory(
        env["store"], env["claude_projects"], env["harnesses"], env["reg"],
        dry_run=False)
    assert status == "skip"
    assert "non-empty" in msg
    # Untouched -- the real content survives.
    assert os.path.isfile(os.path.join(link, "some-real-page.md"))


def _make_managed_copy(link, marker_target, page="page.md"):
    """Fabricate a tier-3 managed copy at `link`: a real dir with our marker
    (recording `marker_target`) plus a content file."""
    os.makedirs(link)
    with open(os.path.join(link, mi.MANAGED_COPY_MARKER), "w",
              encoding="utf-8") as f:
        f.write(marker_target + "\n")
    with open(os.path.join(link, page), "w", encoding="utf-8") as f:
        f.write("copied page")


def test_managed_copy_recognized_as_ok_when_target_matches(env):
    # The #20 bug: a tier-3 copy is a non-empty real dir, so the old code hit
    # "non-empty directory ... resolve by hand" instead of recognizing our own
    # copy. It must now report a clean idempotent "ok".
    link = _link_path(env)
    target = os.path.realpath(os.path.join(env["store"], "projects", "myproj"))
    _make_managed_copy(link, target)

    status, msg = mi.link_project_memory(
        env["store"], env["claude_projects"], env["harnesses"], env["reg"],
        dry_run=False)
    assert status == "ok"
    assert "copy" in msg
    # Left untouched -- no churn on the idempotent path.
    assert os.path.isfile(os.path.join(link, "page.md"))


def test_managed_copy_repointed_when_target_differs(env):
    # Copy whose recorded target is a DIFFERENT (renamed/old) project -> repoint.
    link = _link_path(env)
    _make_managed_copy(link, str(env["root"].parent), page="stale.md")

    status, msg = mi.link_project_memory(
        env["store"], env["claude_projects"], env["harnesses"], env["reg"],
        dry_run=False)
    assert status == "changed"
    assert "repointed" in msg
    # The stale copy was torn down and re-linked (proof the rmtree+relink ran,
    # regardless of which tier _make_link landed on).
    assert not os.path.exists(os.path.join(link, "stale.md"))
    if os.path.islink(link) or mi._is_junction(link):
        assert os.path.realpath(link) == os.path.realpath(
            os.path.join(env["store"], "projects", "myproj"))


def test_managed_copy_dry_run_reports_without_touching(env):
    link = _link_path(env)
    _make_managed_copy(link, str(env["root"].parent), page="stale.md")

    status, msg = mi.link_project_memory(
        env["store"], env["claude_projects"], env["harnesses"], env["reg"],
        dry_run=True)
    assert status == "changed"
    assert "would repoint" in msg
    # Nothing mutated under dry-run.
    assert os.path.isfile(os.path.join(link, "stale.md"))


def test_honors_registry_override_for_project_name(tmp_path, monkeypatch):
    store = tmp_path / "store"
    (store / "projects" / "renamed").mkdir(parents=True)
    claude_projects = tmp_path / "claude_projects"
    claude_projects.mkdir()
    root = tmp_path / "repos" / "original-name"
    root.mkdir(parents=True)
    monkeypatch.setattr(mi, "_current_git_root", lambda: str(root))
    reg = {"overrides": {str(root): "renamed"}}

    status, msg = mi.link_project_memory(
        str(store), str(claude_projects), {"claude_code": "/fake"}, reg,
        dry_run=False)
    assert status == "changed"
    assert "renamed" in msg
    encoded = mi.encode_root(str(root))
    link = os.path.join(str(claude_projects), encoded, "memory")
    assert os.path.realpath(link) == os.path.realpath(
        os.path.join(str(store), "projects", "renamed"))
