import json
import os

import pytest

import memory_init as mi


# ---------------------------------------------------------------------------
# #42 — Antigravity (agy) store-path access pre-grant. The concrete mechanism
# is a TRUST_FOLDER entry in ~/.gemini/trustedFolders.json, scoped to exactly
# the store path (not the global allowNonWorkspaceAccess switch). Tests point
# the trustedFolders path into tmp_path and force agy_present(), so no real
# ~/.gemini is read or written and the leak gate stays green.
# ---------------------------------------------------------------------------

@pytest.fixture
def tf(tmp_path, monkeypatch):
    """A fake trustedFolders.json path under tmp_path, with agy forced present."""
    path = tmp_path / ".gemini" / "trustedFolders.json"
    monkeypatch.setattr(mi, "_agy_trusted_folders_path", lambda: str(path))
    monkeypatch.setattr(mi, "agy_present", lambda: True)
    return path


STORE = "/store/okfmem-store"
# The key the grant/revoke code actually writes: it abspaths the store path, so
# on Windows "/store/okfmem-store" normalizes to "C:\store\okfmem-store". Tests
# must compare against the normalized form, not the raw POSIX literal.
KEY = os.path.abspath(os.path.expanduser(STORE))


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_grant_creates_trust_folder_entry(tf):
    mi.grant_agy_store_access(STORE)
    assert _read(tf)[KEY] == "TRUST_FOLDER"


def test_grant_is_idempotent(tf):
    mi.grant_agy_store_access(STORE)
    first = tf.read_text(encoding="utf-8")
    mi.grant_agy_store_access(STORE)  # second call is a no-op
    assert tf.read_text(encoding="utf-8") == first
    assert _read(tf) == {KEY: "TRUST_FOLDER"}


def test_grant_preserves_other_folders(tf):
    tf.parent.mkdir(parents=True)
    tf.write_text(json.dumps({"/other": "TRUST_PARENT"}), encoding="utf-8")
    mi.grant_agy_store_access(STORE)
    data = _read(tf)
    assert data["/other"] == "TRUST_PARENT"
    assert data[KEY] == "TRUST_FOLDER"


def test_grant_state_probe(tf):
    assert mi.agy_grant_state(STORE) == "ungranted"
    mi.grant_agy_store_access(STORE)
    assert mi.agy_grant_state(STORE) == "granted"


def test_grant_state_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(mi, "agy_present", lambda: False)
    assert mi.agy_grant_state(STORE) == "not-installed"


def test_grant_noop_when_agy_absent(tmp_path, monkeypatch):
    path = tmp_path / ".gemini" / "trustedFolders.json"
    monkeypatch.setattr(mi, "_agy_trusted_folders_path", lambda: str(path))
    monkeypatch.setattr(mi, "agy_present", lambda: False)
    mi.grant_agy_store_access(STORE)
    assert not path.exists()  # nothing written when agy isn't installed


def test_dry_run_writes_nothing(tf):
    mi.grant_agy_store_access(STORE, dry_run=True)
    assert not tf.exists()


def test_revoke_removes_only_our_entry(tf):
    tf.parent.mkdir(parents=True)
    tf.write_text(
        json.dumps({KEY: "TRUST_FOLDER", "/other": "TRUST_PARENT"}),
        encoding="utf-8",
    )
    mi.revoke_agy_store_access(STORE)
    data = _read(tf)
    assert KEY not in data
    assert data["/other"] == "TRUST_PARENT"


def test_revoke_leaves_user_trust_on_store_path(tf):
    # If the store path carries a value we did NOT set (e.g. the user made it a
    # TRUST_PARENT by hand), revoke must not touch it.
    tf.parent.mkdir(parents=True)
    tf.write_text(json.dumps({KEY: "TRUST_PARENT"}), encoding="utf-8")
    mi.revoke_agy_store_access(STORE)
    assert _read(tf) == {KEY: "TRUST_PARENT"}


def test_revoke_noop_when_file_absent(tf):
    mi.revoke_agy_store_access(STORE)  # file never created — must not raise
    assert not tf.exists()


def test_revoke_dry_run_writes_nothing(tf):
    tf.parent.mkdir(parents=True)
    tf.write_text(json.dumps({KEY: "TRUST_FOLDER"}), encoding="utf-8")
    before = tf.read_text(encoding="utf-8")
    mi.revoke_agy_store_access(STORE, dry_run=True)
    assert tf.read_text(encoding="utf-8") == before


def test_corrupt_json_treated_as_empty(tf):
    tf.parent.mkdir(parents=True)
    tf.write_text("{ not valid json", encoding="utf-8")
    # grant must recover (not raise) and produce a clean map
    mi.grant_agy_store_access(STORE)
    assert _read(tf) == {KEY: "TRUST_FOLDER"}
