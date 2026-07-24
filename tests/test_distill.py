"""Coverage for the opt-in `okfmem distill` reflection/distillation reporter (#7).

The load-bearing invariants, asserted here:
  (a) zero API calls / zero mandatory deps — the module is pure-stdlib heuristics
      (no network import exists; nothing is called that could reach a model);
  (b) NO page is created or modified — running distill against a store leaves
      every byte under it untouched (test_distill_never_writes_to_store).

Plus the candidate-detection contract: cross-session recurrence floor, dedup
against existing pages, document-frequency (boilerplate) ceiling, role
filtering, bigram-subsumes-unigram, project scoping, and determinism.
"""
import os
import sys

# The plugin lives under plugins/; put it on the path (conftest only adds ROOT).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins"))

import memory_distill as md  # noqa: E402
from memory_init import encode_root  # noqa: E402  (real inverse of decode_root)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def turn(project, session, text, role="user", harness="agy"):
    # agy harness -> store_project_for uses the basename directly (no fs probe),
    # which keeps the pure-function tests independent of decode_root.
    return {"harness": harness, "project": project, "session_id": session,
            "role": role, "text": text, "tool_name": None}


def slugs(cands):
    return {c["slug"] for c in cands}


# ---------------------------------------------------------------------------
# tokenization / phrase extraction
# ---------------------------------------------------------------------------
def test_tokenize_drops_stopwords_short_and_trailing_punct():
    toks = md._tokenize("The leak gate runs, then check. step.")
    # 'the'/'then' stopwords gone; 'runs'->stopword; trailing dots stripped
    assert "leak" in toks and "gate" in toks
    assert "check" in toks and "step" in toks   # not "check."/"step."
    assert "the" not in toks and "then" not in toks


def test_tokenize_requires_a_letter():
    assert md._tokenize("v1 2026 123 abc") == ["abc"]


def test_slugify_produces_clean_kebab():
    assert md._slugify(("commit", "message")) == "commit-message"
    assert md._slugify(("skill.md",)) == "skill-md"        # dot collapses
    assert md._slugify(("github.com",)) == "github-com"


def test_phrases_bigrams_only_span_adjacent_kept_tokens():
    phrases = set(md._phrases("leak gate but decay scoring"))
    assert ("leak", "gate") in phrases          # adjacent kept tokens
    assert ("decay", "scoring") in phrases
    assert ("gate", "decay") not in phrases     # 'but' (stopword) breaks the run
    assert ("leak",) in phrases and ("scoring",) in phrases


# ---------------------------------------------------------------------------
# collect_candidates: recurrence floor
# ---------------------------------------------------------------------------
def test_recurrence_floor_needs_min_sessions():
    turns = [turn("p", "s1", "widget pipeline design"),
             turn("p", "s2", "widget pipeline design")]  # only 2 sessions
    got = md.collect_candidates(turns, {"p": []}, {}, min_sessions=3)
    assert got == {}  # below floor -> nothing proposed

    turns.append(turn("p", "s3", "widget pipeline design"))
    got = md.collect_candidates(turns, {"p": []}, {}, min_sessions=3)
    assert "widget-pipeline" in slugs(got["p"])


# ---------------------------------------------------------------------------
# dedup against existing pages
# ---------------------------------------------------------------------------
def test_covered_phrase_is_deduped_out():
    turns = [turn("p", f"s{i}", "leak gate scanning") for i in range(3)]
    # an existing page whose token set already covers {leak, gate}
    coverage = {"p": [{"leak", "gate", "scanning"}]}
    got = md.collect_candidates(turns, coverage, {}, min_sessions=3)
    assert "leak-gate" not in slugs(got.get("p", []))
    # but a genuinely novel phrase in the same turns survives
    turns2 = [turn("p", f"s{i}", "leak gate and novel topic") for i in range(3)]
    got2 = md.collect_candidates(turns2, coverage, {}, min_sessions=3)
    assert "novel-topic" in slugs(got2["p"])


# ---------------------------------------------------------------------------
# document-frequency (boilerplate) ceiling
# ---------------------------------------------------------------------------
def test_boilerplate_ceiling_drops_near_ubiquitous_phrases():
    turns = []
    for i in range(10):                      # 10 distinct sessions >= MAX_RATIO_MIN_SESS
        turns.append(turn("p", f"s{i}", "injected boilerplate banner"))
    for i in range(3):                       # a real topic in only 3 of them
        turns.append(turn("p", f"s{i}", "actual design decision"))
    got = md.collect_candidates(turns, {"p": []}, {},
                                min_sessions=3, max_ratio=0.5)
    got_slugs = slugs(got["p"])
    assert "injected-boilerplate" not in got_slugs   # in 10/10 -> boilerplate
    assert "actual-design" in got_slugs              # in 3/10 -> topic


def test_ceiling_inactive_on_small_corpus():
    # below MAX_RATIO_MIN_SESS the floor governs; a phrase in all 3 sessions is
    # NOT dropped as boilerplate (there is no valid band yet).
    turns = [turn("p", f"s{i}", "small corpus topic") for i in range(3)]
    got = md.collect_candidates(turns, {"p": []}, {}, min_sessions=3, max_ratio=0.5)
    assert "small-corpus" in slugs(got["p"])


# ---------------------------------------------------------------------------
# role filtering
# ---------------------------------------------------------------------------
def test_tool_and_thinking_roles_excluded_by_default():
    turns = [turn("p", f"s{i}", "signature noise here", role="tool") for i in range(3)]
    turns += [turn("p", f"s{i}", "inner monologue here", role="thinking") for i in range(3)]
    got = md.collect_candidates(turns, {"p": []}, {}, min_sessions=3)
    assert got == {}  # neither role carries topic prose by default
    # explicitly opting the roles in surfaces them
    got2 = md.collect_candidates(turns, {"p": []}, {}, min_sessions=3,
                                 roles={"tool", "thinking"})
    assert got2 != {}


# ---------------------------------------------------------------------------
# bigram subsumes unigram
# ---------------------------------------------------------------------------
def test_bigram_subsumes_component_unigrams():
    turns = [turn("p", f"s{i}", "decay scoring") for i in range(3)]
    got = md.collect_candidates(turns, {"p": []}, {}, min_sessions=3)
    got_slugs = slugs(got["p"])
    assert "decay-scoring" in got_slugs
    assert "decay" not in got_slugs and "scoring" not in got_slugs


# ---------------------------------------------------------------------------
# project scoping
# ---------------------------------------------------------------------------
def test_unknown_project_excluded_unless_all_projects():
    turns = [turn("newproj", f"s{i}", "greenfield topic") for i in range(3)]
    # newproj is not a coverage key -> excluded by default
    assert md.collect_candidates(turns, {}, {}, min_sessions=3) == {}
    got = md.collect_candidates(turns, {}, {}, min_sessions=3, all_projects=True)
    assert "greenfield-topic" in slugs(got["newproj"])


def test_store_project_for_agy_uses_basename():
    assert md.store_project_for("agy", "myrepo", {}) == "myrepo"


def test_store_project_for_claude_uses_registry_then_basename(tmp_path):
    repo = tmp_path / "demoproj"
    repo.mkdir()
    token = encode_root(str(repo))                 # engine's encoder (POSIX + Windows)
    # registry hit wins
    assert md.store_project_for("claude-code", token,
                                {str(repo): "mapped"}) == "mapped"
    # no registry -> decode_root probes the real dir -> basename
    assert md.store_project_for("claude-code", token, {}) == "demoproj"


# ---------------------------------------------------------------------------
# determinism / idempotency
# ---------------------------------------------------------------------------
def test_deterministic_across_runs():
    turns = [turn("p", f"s{i}", "alpha beta gamma decision") for i in range(4)]
    a = md.collect_candidates(list(turns), {"p": []}, {}, min_sessions=3)
    b = md.collect_candidates(list(turns), {"p": []}, {}, min_sessions=3)
    assert a == b


# ---------------------------------------------------------------------------
# load_coverage
# ---------------------------------------------------------------------------
def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_load_coverage_reads_pages_and_skips_index_files(tmp_path):
    store = str(tmp_path)
    pdir = os.path.join(store, "projects", "demoproj")
    _write(os.path.join(pdir, "leak-gate.md"),
           "---\ntype: project\n---\n\n# The leak gate\n\nbody\n")
    _write(os.path.join(pdir, "MEMORY.md"),
           "# MEMORY\n\n- [Leak gate](leak-gate.md) — payload scoped scanner\n")
    _write(os.path.join(pdir, "STATE.md"), "# state\n")  # must be skipped
    cov = md.load_coverage(store)
    assert "demoproj" in cov
    assert len(cov["demoproj"]) == 1  # only leak-gate.md, not MEMORY/STATE
    page = cov["demoproj"][0]
    assert {"leak", "gate"} <= page          # slug + H1
    assert "scanner" in page                  # from the MEMORY.md hook


# ---------------------------------------------------------------------------
# THE gate invariant: distill never writes to the store
# ---------------------------------------------------------------------------
def _snapshot(root):
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            with open(p, "rb") as f:
                snap[p] = f.read()
    return snap


def test_distill_never_writes_to_store(tmp_path, monkeypatch, capsys):
    # Real store with an existing page + MEMORY.md.
    store = tmp_path / "store"
    pdir = store / "projects" / "demoproj"
    pdir.mkdir(parents=True)
    _write(str(pdir / "known.md"),
           "---\ntype: project\n---\n\n# Known thing\n\nbody\n")
    _write(str(pdir / "MEMORY.md"), "# MEMORY\n\n- [Known](known.md) — hook\n")

    # A Claude transcript whose project encodes to demoproj, with a recurring
    # topic across 3 sessions that has no page yet.
    repo = tmp_path / "demoproj"
    repo.mkdir()
    # Encode exactly as Claude Code does — via the engine's encode_root, the
    # real inverse of decode_root (which distill uses to map the token back).
    # A hand-rolled str.replace("/", "-") is wrong on Windows: str(WindowsPath)
    # uses "\" and keeps the drive colon, so the token stays an absolute path
    # and `croot / token` collapses to `repo` itself (FileExistsError).
    token = encode_root(str(repo))
    croot = tmp_path / "claude"
    proj_dir = croot / token
    proj_dir.mkdir(parents=True)
    import json
    for i in range(3):
        lines = []
        for _ in range(2):
            lines.append(json.dumps({
                "type": "user", "sessionId": f"sess{i}", "timestamp": "2026-07-23",
                "message": {"role": "user",
                            "content": "we keep hitting the widget pipeline problem"},
            }))
        _write(str(proj_dir / f"sess{i}.jsonl"), "\n".join(lines) + "\n")

    before = _snapshot(str(store))

    argv = ["distill", "--store", str(store), "--claude-root", str(croot),
            "--agy-path", str(tmp_path / "nope.jsonl"), "--min-sessions", "3"]
    monkeypatch.setattr(sys, "argv", ["okfmem distill"] + argv)
    md.main()
    out = capsys.readouterr().out

    # the recurring, uncovered topic is proposed ...
    assert "widget-pipeline" in out
    # ... report says it wrote nothing ...
    assert "writes nothing" in out
    # ... and the store is byte-for-byte unchanged: no page created or modified.
    assert _snapshot(str(store)) == before


def test_module_has_no_network_imports():
    # Invariant (a): the default path cannot make an API call. Assert the source
    # imports nothing that could reach the network / a model.
    src = open(md.__file__, encoding="utf-8").read()
    for banned in ("import requests", "import http", "import urllib",
                   "import socket", "anthropic", "openai"):
        assert banned not in src, f"unexpected network-capable import: {banned}"
