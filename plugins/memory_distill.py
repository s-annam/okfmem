#!/usr/bin/env python3
"""okfmem distill — reflection + distillation reporter (OPT-IN plugin, issue #7).

The "consolidation v1.1" step. Reads the SAME normalized session-turn corpus that
`okfmem search` indexes (adapters/claude_code + agy — layer-2 scrubbed,
tool-result-free), finds topics/decisions that RECUR across multiple sessions yet
have NO corresponding durable page, and emits a *proposed pages* report.

Two hard invariants (issue #7), enforced by construction:

  (a) DEFAULT PATH MAKES ZERO API CALLS AND ADDS ZERO MANDATORY DEPS. Candidate
      detection is pure-Python heuristics (frequency + session-recurrence, diffed
      against existing pages). No model is called to PRODUCE this report; the
      report is the FEED to a model-at-session-end or a human, who is the gate.
      Not wired into the headless Stop-hook consolidation job, so that job keeps
      running with no model and no cost.

  (b) NO PAGE IS EVER CREATED OR MODIFIED HERE. This module is a pure reporter:
      it opens the store read-only and writes nothing under it. Ever. The gate
      (model at session end, or human review) is downstream — it reads this
      report and decides what to author. A dry-run reporter, by design, not a
      writer with a dry-run flag.

Reached only via the dispatcher's lazy plugin route (`okfmem distill ...`),
never from MODULES, so `okfmem consolidate` / `okfmem status` stay LLM-free.

Usage:
  okfmem distill [--project NAME] [--min-sessions N] [--limit N] [--json]
                 [--all-projects] [--store PATH] [--claude-root PATH]
                 [--agy-path PATH]

Output = candidate durable pages, grouped by project, each with a suggested slug,
the recurring phrase, how many distinct sessions touched it, and example session
ids. Deterministic (stable sort) so re-runs on the same corpus are idempotent.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# adapters + shared session lib live under plugins/; memory_init (decode_root)
# lives at the repo root. Put both on the path, mirroring memory_search.py.
_PLUGINS_DIR = os.path.dirname(os.path.realpath(__file__))
_ROOT_DIR = os.path.dirname(_PLUGINS_DIR)
sys.path.insert(0, _PLUGINS_DIR)
sys.path.insert(0, _ROOT_DIR)
from adapters import agy, claude_code  # noqa: E402
from memory_init import decode_root  # noqa: E402  (reuse the FS-probing decoder)

DEFAULT_STORE = os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store"))

# Tunables (all overridable on the CLI).
MIN_SESSIONS = 3    # a topic must recur across >= this many DISTINCT sessions
LIMIT = 12          # max proposals surfaced per project
MIN_TOKEN_LEN = 3   # ignore very short tokens

# Document-frequency ceiling: a phrase in more than this fraction of a project's
# distinct sessions is structural boilerplate (slash-command wrappers, the
# per-session "local commands" caveat, system-reminder text) — recurring, but
# not a distinctive TOPIC. IDF-style pruning; generic, so no product-specific
# strings are hardcoded. Only applied once a project has enough sessions that a
# ceiling and the min-sessions floor leave a real band (see MAX_RATIO_MIN_SESS).
MAX_SESSION_RATIO = 0.5
MAX_RATIO_MIN_SESS = 8   # below this many sessions, rely on the floor alone

# Roles that carry TOPIC prose. `tool` turns are command/path signatures
# (file paths, git URLs, tempfiles) and `thinking` is verbose inner mechanics —
# both are noise for topic detection, so they're excluded by default. Genuine
# decisions/topics live in what the user and the assistant SAY.
DEFAULT_ROLES = frozenset({"user", "assistant"})

# Pages/index files that are not durable knowledge pages.
_SKIP_PAGE_NAMES = {"MEMORY.md", "STATE.md", "CONTEXT.md", "SESSIONS.md", "README.md"}

# Token = a word-ish run bounded by alphanumerics, with tech-y inner chars kept
# (node.js, memory_init.py, github.com), lowercased. Requiring alnum edges drops
# trailing punctuation ("step." -> "step") and stray separators. A token must
# also contain a letter (see _tokenize) so bare numbers/versions are ignored.
_TOKEN_RE = re.compile(r"[a-z0-9](?:[a-z0-9_.+#-]*[a-z0-9])?")
_HAS_ALPHA_RE = re.compile(r"[a-z]")
# Sentence/phrase boundaries for bigram formation: punctuation that ENDS a
# clause (a `.`/`,`/`;` etc. only when followed by whitespace or end — so
# "node.js" and "github.com" stay whole) plus brackets/quotes/pipes/newlines.
# Whitespace is deliberately NOT a boundary, so adjacent words within a clause
# can pair into a bigram.
_SEGMENT_RE = re.compile(r"[.!?;:,]+(?=\s)|[.!?;:,]+$|[()\[\]{}<>\"'`|/\\\n\r\t]+")
_H1_RE = re.compile(r"^#\s+(.*?)\s*$", re.MULTILINE)

# Generic English + ubiquitous dev/harness noise. Deliberately does NOT include
# okfmem-domain words (memory, store, decay, page, sync, hook, ...) — those ARE
# the signal for this repo, and the dedup-against-existing-pages pass is what
# keeps already-covered domain topics out of the report.
_STOPWORDS = frozenset("""
a an and the of to in on at for is it its be this that these those with as by from
into over under again further then once here there all any both each few more most
other some such no nor not only own same so than too very can will just should now
i you he she we they them his her our your their me my mine ours yours theirs who whom
what which whose where when why how do does did doing done have has had having are was
were been being am if or but because while about against between through during before
after above below up down out off out again new old get got getting go going goes went
gone make makes made making let lets letting use used using uses need needs want wants
add adds added adding fix fixes fixed fixing change changes changed run runs running ran
see sees saw seen look looks looking like likes way ways thing things one two three
also into via per etc eg ie vs okay ok yes yeah sure thanks please would could
file files code line lines function functions method methods case cases test tests
value values name names type types call calls set sets return returns print prints
bash read edit write grep glob tool tools user assistant thinking null true false
""".split())


# ---------------------------------------------------------------------------
# tokenization + phrase extraction
# ---------------------------------------------------------------------------
def _keep_token(t):
    return (len(t) >= MIN_TOKEN_LEN and t not in _STOPWORDS
            and _HAS_ALPHA_RE.search(t) is not None)


def _tokenize(text):
    """Lowercase token list, length-, alpha- and stopword-filtered."""
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if _keep_token(t)]


def _phrases(text):
    """Yield candidate phrases (unigrams + adjacent bigrams) from one text.

    Bigrams are formed only between tokens ADJACENT in the original stream once
    stopwords/punctuation are removed within a run — so "leak gate", "decay
    scoring", "dirty tree" survive as pairs, but words separated by a stopword
    or a sentence break do not glue together. We split the raw text on any run
    of non-token chars first, so a period / newline / stopword genuinely breaks
    adjacency (segment boundaries are natural phrase boundaries).
    """
    for segment in _SEGMENT_RE.split((text or "").lower()):
        run = []  # contiguous kept tokens; a dropped token flushes the run
        for raw in _TOKEN_RE.findall(segment):
            if _keep_token(raw):
                run.append(raw)
            else:
                yield from _emit_run(run)
                run = []
        yield from _emit_run(run)


def _emit_run(run):
    for t in run:
        yield (t,)
    for a, b in zip(run, run[1:]):
        yield (a, b)


# ---------------------------------------------------------------------------
# existing-page coverage (dedup source)
# ---------------------------------------------------------------------------
def _page_token_set(store, proj, page_name, memory_hook=""):
    """Token set representing what an existing page already covers: its slug +
    H1 title + its MEMORY.md hook line. Used to suppress already-covered topics."""
    tokens = set(_tokenize(page_name[:-3].replace("-", " ").replace("_", " ")))
    path = os.path.join(store, "projects", proj, page_name)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(4000)
    except OSError:
        head = ""
    m = _H1_RE.search(head)
    if m:
        tokens |= set(_tokenize(m.group(1)))
    tokens |= set(_tokenize(memory_hook))
    return tokens


def _memory_hooks(store, proj):
    """Map slug.md -> its MEMORY.md hook text (the `- [Title](slug.md) — hook`
    line), best-effort. Empty when no MEMORY.md."""
    hooks = {}
    path = os.path.join(store, "projects", proj, "MEMORY.md")
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return hooks
    for ln in text.splitlines():
        for m in re.finditer(r"[(/]([A-Za-z0-9._-]+)\.md\)", ln):
            hooks[m.group(1) + ".md"] = ln
    return hooks


def load_coverage(store, projects=None):
    """{store_project: [token_set_per_page]} for the given (or all) projects.

    A project with a dir but no pages yields []; a project absent from the store
    yields no key at all (callers decide whether to still report it)."""
    proj_root = os.path.join(store, "projects")
    coverage = {}
    if not os.path.isdir(proj_root):
        return coverage
    names = projects if projects is not None else sorted(
        d for d in os.listdir(proj_root)
        if os.path.isdir(os.path.join(proj_root, d)))
    for proj in names:
        pdir = os.path.join(proj_root, proj)
        if not os.path.isdir(pdir):
            continue
        hooks = _memory_hooks(store, proj)
        page_sets = []
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".md") or fn in _SKIP_PAGE_NAMES:
                continue
            if fn.startswith("ck_"):
                continue
            page_sets.append(_page_token_set(store, proj, fn, hooks.get(fn, "")))
        coverage[proj] = page_sets
    return coverage


def _covered(phrase_tokens, page_sets):
    """True iff some single existing page already covers ALL of the phrase's
    tokens — i.e. the concept lives in one page, so proposing it would dupe."""
    want = set(phrase_tokens)
    return any(want <= ps for ps in page_sets)


# ---------------------------------------------------------------------------
# project resolution: harness token -> store project name
# ---------------------------------------------------------------------------
def _load_registry_map(store):
    try:
        with open(os.path.join(store, "registry.json"), "r", encoding="utf-8") as f:
            reg = json.load(f)
        return reg.get("map", {}) if isinstance(reg, dict) else {}
    except (OSError, ValueError):
        return {}


def store_project_for(harness, token, registry_map):
    """Map a corpus turn's `project` field to a store project name.

    claude-code: the token is Claude's encoded cwd (`-Users-you-proj`); decode
    (filesystem-probed) then registry-map, falling back to basename. agy: the
    token is already a workspace basename — use it directly."""
    if harness == "claude-code":
        path = decode_root(token)
        return registry_map.get(path) or os.path.basename(path.rstrip("/")) or token
    return os.path.basename((token or "").rstrip("/")) or token


# ---------------------------------------------------------------------------
# corpus -> candidates
# ---------------------------------------------------------------------------
def iter_corpus(claude_root, agy_path, project=None):
    """Yield normalized turns from both adapters (the search corpus, unindexed).
    `project` here filters the HARNESS token, matching the adapters' own filter;
    store-project scoping happens after mapping, in collect_candidates."""
    yield from claude_code.iter_turns(claude_root, project=project)
    yield from agy.iter_turns(agy_path, project=project)


def _slugify(phrase_tokens):
    """Clean kebab-case slug: inner dots/underscores (github.com, skill.md)
    collapse to dashes so a proposed page name is a plain slug."""
    raw = "-".join(phrase_tokens)
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", raw)).strip("-")[:60]


def collect_candidates(turns, coverage, registry_map, *,
                       min_sessions=MIN_SESSIONS, limit=LIMIT,
                       max_ratio=MAX_SESSION_RATIO, roles=DEFAULT_ROLES,
                       all_projects=False):
    """Pure core: turns -> {store_project: [candidate dict]}.

    A candidate is a phrase that (1) recurs across >= min_sessions DISTINCT
    sessions within a store project, (2) is NOT structural boilerplate (present
    in > max_ratio of that project's sessions, once the project is big enough —
    MAX_RATIO_MIN_SESS), and (3) is not already covered by an existing page
    there. Ranked by session-recurrence (primary), bigram-first (multi-word
    phrases carry more topic signal), then total occurrences.

    coverage: {store_project: [page_token_set]}. A project present as a key is
    "known" (has a store dir). By default only known projects are considered;
    with all_projects=True, an unknown project is treated as having no pages
    (every recurring topic is a candidate) — useful for bootstrapping a project
    that has no durable pages yet.
    """
    # phrase stats per store project: phrase -> {"sessions": set, "count": int}
    stats = {}
    proj_sessions = {}  # store_project -> set of distinct session ids (for DF ceiling)
    for t in turns:
        if roles is not None and t.get("role") not in roles:
            continue
        proj = store_project_for(t.get("harness"), t.get("project"), registry_map)
        if proj not in coverage and not all_projects:
            continue
        sess = t.get("session_id")
        proj_sessions.setdefault(proj, set()).add(sess)
        pstats = stats.setdefault(proj, {})
        for ph in _phrases(t.get("text")):
            rec = pstats.get(ph)
            if rec is None:
                pstats[ph] = {"sessions": {sess}, "count": 1}
            else:
                rec["sessions"].add(sess)
                rec["count"] += 1

    out = {}
    for proj, pstats in stats.items():
        page_sets = coverage.get(proj, [])
        total_sessions = len(proj_sessions.get(proj, ()))
        # ceiling only kicks in once the corpus is large enough to have a band
        ceiling = (int(max_ratio * total_sessions)
                   if total_sessions >= MAX_RATIO_MIN_SESS else None)
        cands = []
        for ph, rec in pstats.items():
            n_sessions = len(rec["sessions"])
            if n_sessions < min_sessions:
                continue
            if ceiling is not None and n_sessions > ceiling:
                continue  # structural boilerplate — recurs almost everywhere
            if _covered(ph, page_sets):
                continue
            cands.append({
                "project": proj,
                "phrase": " ".join(ph),
                "slug": _slugify(ph),
                "sessions": n_sessions,
                "occurrences": rec["count"],
                "is_bigram": len(ph) > 1,
                "example_sessions": sorted(
                    str(s)[:8] for s in rec["sessions"] if s)[:3],
            })
        # Drop unigram candidates fully subsumed by a surviving bigram (the
        # bigram is the better proposal; keep the report from listing both
        # "leak" and "leak gate"). Deterministic.
        bigram_tokens = {tuple(c["phrase"].split()) for c in cands if c["is_bigram"]}
        subsumed = {tok for bg in bigram_tokens for tok in bg}
        cands = [c for c in cands
                 if c["is_bigram"] or c["phrase"] not in subsumed]
        cands.sort(key=lambda c: (-c["sessions"], not c["is_bigram"],
                                  -c["occurrences"], c["phrase"]))
        if cands:
            out[proj] = cands[:limit]
    return out


# ---------------------------------------------------------------------------
# rendering (stdout only — never touches the store)
# ---------------------------------------------------------------------------
def render_text(report, min_sessions):
    lines = []
    total = sum(len(v) for v in report.values())
    lines.append("okfmem distill — proposed durable pages (report only; writes nothing)")
    lines.append(f"gate: a model at session end (/okfmem-save) or a human reviews "
                 f"these and authors/updates pages. {total} candidate(s), "
                 f"recurring across >= {min_sessions} sessions.")
    if not report:
        lines.append("")
        lines.append("no candidates — no cross-session topic lacks a durable page.")
        return "\n".join(lines)
    for proj in sorted(report):
        lines.append("")
        lines.append(f"## {proj}")
        for c in report[proj]:
            kind = "phrase" if c["is_bigram"] else "term"
            ex = ", ".join(c["example_sessions"])
            lines.append(
                f"  - [{c['slug']}.md]  {kind}: \"{c['phrase']}\"  "
                f"({c['sessions']} sessions, {c['occurrences']} hits; e.g. {ex})")
    return "\n".join(lines)


def build_report(args):
    store = os.path.abspath(os.path.expanduser(args.store))
    only = [args.project] if args.project else None
    coverage = load_coverage(store, projects=only)
    registry_map = _load_registry_map(store)
    turns = iter_corpus(os.path.expanduser(args.claude_root),
                        os.path.expanduser(args.agy_path))
    roles = (frozenset(r.strip() for r in args.roles.split(",") if r.strip())
             if args.roles else DEFAULT_ROLES)
    report = collect_candidates(
        turns, coverage, registry_map,
        min_sessions=args.min_sessions, limit=args.limit,
        max_ratio=args.max_session_ratio, roles=roles,
        all_projects=args.all_projects)
    if args.project:
        report = {k: v for k, v in report.items() if k == args.project}
    return report


def main():
    p = argparse.ArgumentParser(
        prog="okfmem distill", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--store", default=DEFAULT_STORE)
    p.add_argument("--project", help="limit to one store project name")
    p.add_argument("--min-sessions", type=int, default=MIN_SESSIONS,
                   help=f"recurrence threshold (default {MIN_SESSIONS})")
    p.add_argument("--limit", type=int, default=LIMIT,
                   help=f"max proposals per project (default {LIMIT})")
    p.add_argument("--max-session-ratio", type=float, default=MAX_SESSION_RATIO,
                   help="drop phrases in > this fraction of a project's sessions "
                        f"as boilerplate (default {MAX_SESSION_RATIO})")
    p.add_argument("--roles", default=None,
                   help="comma-separated turn roles to mine "
                        f"(default: {','.join(sorted(DEFAULT_ROLES))})")
    p.add_argument("--all-projects", action="store_true",
                   help="include harness projects with no store dir yet")
    p.add_argument("--json", action="store_true", help="emit JSON, not text")
    p.add_argument("--claude-root", default=claude_code.DEFAULT_ROOT)
    p.add_argument("--agy-path", default=agy.DEFAULT_PATH)

    # Dispatcher hands us `distill ...`; drop that leading verb token if present.
    argv = sys.argv[1:]
    if argv and argv[0] == "distill":
        argv = argv[1:]
    args = p.parse_args(argv)

    report = build_report(args)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report, args.min_sessions))


if __name__ == "__main__":
    main()
