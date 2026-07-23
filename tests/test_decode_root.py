import os
import sys

import pytest

import memory_init as mi


def _encode(path):
    """Mirror Claude Code's cwd -> project-dir encoding: replace '/' with '-'."""
    return path.replace(os.sep, "-")


def test_simple_posix_path_round_trips(tmp_path):
    project = tmp_path / "myproject"
    project.mkdir()
    encoded = _encode(str(project))
    assert mi.decode_root(encoded) == os.path.normpath(str(project))


def test_hyphenated_directory_name_round_trips(tmp_path):
    # The whole point of decode_root: directory names containing '-' must
    # still resolve correctly by greedily probing the filesystem.
    project = tmp_path / "worktree-autosync"
    project.mkdir()
    encoded = _encode(str(project))
    assert mi.decode_root(encoded) == os.path.normpath(str(project))


def test_nested_hyphenated_directories_round_trip(tmp_path):
    nested = tmp_path / "my-cool-project" / "sub-dir-here"
    nested.mkdir(parents=True)
    encoded = _encode(str(nested))
    assert mi.decode_root(encoded) == os.path.normpath(str(nested))


def test_unresolvable_tail_falls_back_to_verbatim_reconstruction(tmp_path):
    # A path whose leaf doesn't exist on disk still reconstructs sensibly
    # (this is the existing greedy-probe fallback, unchanged by the Windows
    # drive-letter work).
    encoded = _encode(str(tmp_path)) + "-does-not-exist"
    result = mi.decode_root(encoded)
    assert result == os.path.normpath(
        os.path.join(str(tmp_path), "does-not-exist"))


@pytest.mark.skipif(sys.platform == "win32",
                    reason="asserts the POSIX (no-op) behavior specifically")
def test_windows_drive_letter_helper_is_noop_on_real_posix():
    # On an actual POSIX machine, os.name is "posix", so the helper must
    # return None regardless of what tokens look like.
    assert mi._windows_drive_root(["C", "Users", "name", "project"]) is None


def test_windows_drive_letter_detected(monkeypatch):
    # Simulate the Windows branch portably: force os.name to "nt" and stub
    # os.path.isdir so the helper believes "C:\\" exists, without requiring
    # an actual Windows filesystem. A live-Windows integration case is below,
    # gated to only run on a real Windows runner (CI's windows-latest).
    monkeypatch.setattr(mi.os, "name", "nt")
    real_isdir = mi.os.path.isdir

    def fake_isdir(path):
        return True if path == "C:\\" else real_isdir(path)

    monkeypatch.setattr(mi.os.path, "isdir", fake_isdir)

    assert mi._windows_drive_root(["C", "Users", "name", "project"]) == ("C:\\", 1)


def test_windows_drive_letter_rejects_non_drive_first_token(monkeypatch):
    monkeypatch.setattr(mi.os, "name", "nt")
    assert mi._windows_drive_root(["Users", "name", "project"]) is None
    assert mi._windows_drive_root([]) is None


def test_windows_drive_letter_returns_none_when_drive_missing(monkeypatch):
    monkeypatch.setattr(mi.os, "name", "nt")
    monkeypatch.setattr(mi.os.path, "isdir", lambda path: False)
    assert mi._windows_drive_root(["Z", "Users", "name"]) is None


@pytest.mark.skipif(sys.platform != "win32",
                    reason="requires a real Windows filesystem")
def test_decode_root_with_real_windows_drive():
    # Only meaningful on an actual Windows runner (CI's windows-latest).
    encoded = "C-Users-doesnotexist-project"
    result = mi.decode_root(encoded)
    assert result.lower().startswith("c:\\")
