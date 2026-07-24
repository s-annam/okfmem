---
name: implement-batch
description: Implement a set of GitHub issues (an epic's sub-issues, or an explicit list) onto one branch as a single accumulated commit, delegating each issue to an isolated subagent, then run an adversarial review pass and open one PR via /open-pr. Use when the user says "implement batch", "/implement-batch", "implement these issues", or hands you an epic/parent issue to build end-to-end.
argument-hint: <PARENT#> | <#,#,#> [--no-review | --review=N] [--from <#>] [--order <#,#,...>] [--no-commit]
---

# Implement Batch

Orchestrate implementing a **set of GitHub issues** — the sub-issues of an epic,
or an explicit list — onto **one branch**, accumulating their changes into a
**single commit**, then hardening the result with an **adversarial review pass**
and opening **one PR** via `/open-pr`.

This is a **GitHub-only, self-contained** batch driver for `s-annam/okfmem`. It
has **no Linear code** and **no dependency on a global `/implement-issue`** — the
per-issue implementation contract is embedded here, in the subagent spawn prompt,
so this skill works for anyone who clones `okfmem` (contributors included), not
just a maintainer whose `~/tools/skills/` has the global skills.

> **Why a subagent per issue.** The orchestrator can't `/compact` mid-run, and a
> multi-issue run would blow the main context. Running each issue inside its own
> subagent **is** the context-isolation mechanism: the subagent's heavy
> explore/edit context stays down there; only a tight structured summary returns.
> The orchestrator stays lean across the whole sequence.

## Repo facts (okfmem)

- **Repo:** `s-annam/okfmem`, a **public** repo. `main` is protected — every
  change merges through a PR that needs a green **`verify`** check, **linear
  history**, and **1 approving review from a Code Owner** (`.github/CODEOWNERS`).
  Direct commits/pushes to `main` are blocked. So this skill **never commits on
  `main`** — it works on one feature branch and finalizes through `/open-pr`.
  (While the repo is solo, the owner can't self-approve, so they merge via admin
  bypass once `verify` is green; that bypass ends when a co-maintainer joins.)
- **Public-repo leak rule is non-negotiable** — this replaces the usual
  fixture-PII rule. Never let a private string into a tracked file: a real home
  path (`/Users/<name>`), a `claude.ai/code/session_…` URL, a `Claude-Session:`
  trailer, a personal email, or private store/session contents. Use the
  documented placeholders (`~/okfmem-store`, `$OKFMEM_STORE`, `/Users/you`). The
  gate is `python3 scripts/check-leaks.py` — it scans tracked-file *content* and
  exits non-zero naming `file:line`. It runs first in CI (hard fail) and again in
  `/open-pr`'s preflight (Step 3.5); flag any new private-looking prose the moment
  a subagent reports it, since the gate **cannot** judge whether prose is
  otherwise private.
- **Gates:** `python3 scripts/check-leaks.py` (**hard**) · `python3 -m pytest
  tests/` · `ruff check .` (advisory) · `ruff format --check .` (advisory). CI's
  `verify` runs the whole mirror across an OS matrix. Per issue, run only the
  **fast/affected** checks — the leak gate + the affected `pytest` files + `ruff
  check` on the touched files. The full matrix `verify` is the PR gate (CI), not
  a per-issue gate. There is **no typecheck / build / fallow step** — this is
  pure-Python stdlib.
- **Confirmation discipline (okfmem-specific, three-rung ladder).** okfmem's own
  scripts must never silently mutate outward or user-owned state, and never
  destroy without a typed confirmation. If an issue's change adds/edits a
  state-changing op, the implementing subagent MUST place it on the right rung
  (read-only/additive → no friction; outward/user-config mutating → `[y/N]` +
  skippable-non-interactively with a printed manual command; destructive → typed
  confirmation, never a bare `-Force`). See `CLAUDE.md` → "Confirmation
  discipline". Treat a violation as a **blocking** review finding.
- **Reviewer agent:** `ecc:python-reviewer` (pure-Python repo), falling back to
  `ecc:code-reviewer`. These are **maintainer-global** (`~/.claude/agents/`), not
  in this repo — a fresh clone won't have them. On the default (review-on) path,
  if no `ecc:*` reviewer resolves, fall back to a **`general-purpose`** subagent
  driving the built-in `/code-review` skill; a fresh clone that wants to skip
  review entirely passes `--no-review`.
- **Cross-platform.** okfmem ships `.ps1`/`.cmd` wrappers and Windows-aware path
  code. If an issue touches path handling, encoding, or a shipped `.ps1`, the
  subagent must respect the codebase's Windows invariants (pure-ASCII `.ps1`,
  path decisions by shape not `isdir`, expected path-assertion values from the
  code's own normalizer). Treat a hand-typed POSIX literal in a path assertion as
  a **blocking** finding — it fails Windows-only CI.

## Input

Parse `$ARGUMENTS` for **either**:
- A **parent/epic issue number** (e.g. `50`, `#50`) — discover its GitHub
  **sub-issues** and order them by dependency.
- An **explicit comma-separated list** (e.g. `47,48,50`) — used as the set;
  order still verified against dependencies.

Resolve `<owner>/<repo>` once:
```bash
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"   # s-annam/okfmem
```

**Flags** (strip before parsing the identifier):
- `--no-review` — opt out of the adversarial review pass (Phase 4). On by
  default. Skip only for a trivial/mechanical batch.
- `--review=N` — set the review loop's round cap (default `2`). Mutually
  exclusive with `--no-review`.
- `--from <#>` — resume: skip issues up to `<#>` and start the loop there (the
  branch and earlier issues' changes are assumed already present). See Resume.
- `--order <#,#,...>` — override the computed order explicitly (still validated
  against `blocked_by` edges; warn if it violates one).
- `--no-commit` — leave the accumulated changes uncommitted on the branch for
  the user to review; skip the `/open-pr` finalize. Default is one PR at the end.

## Process

### Phase 1 — Resolve and order the set

1. **Resolve the issue set.**
   - **Parent given** — list its native sub-issues:
     ```bash
     gh api --paginate repos/$REPO/issues/<PARENT>/sub_issues --jq '.[].number'
     ```
     If the parent has no native sub-issues but lists child `#N`s in its body,
     parse those instead. If you can't resolve a child set, **stop and ask**.
   - **Explicit list** — use it as given.
2. **Fetch each issue** (title, state, body) and its **dependencies**:
   ```bash
   gh issue view <N> --repo $REPO --json number,title,state,body
   gh api --paginate repos/$REPO/issues/<N>/dependencies/blocked_by --jq '.[].number'
   ```
3. **Topologically sort by `blocked_by`** — if A blocks B (B is `blocked_by` A),
   A runs before B. Preserve the given order among issues with no edge between
   them. **On a cycle, stop and report it** — don't guess.
4. **Drop already-closed issues** from the run, but list them as skipped.
5. **Decide each issue's effort tier** and **present the plan for explicit
   confirmation** — this is a long autonomous run, so the gate is mandatory.
   Tier heuristic: correctness-critical/ambiguous logic (path encoding, sync
   rebase/lock, badge classification, decay math) → `ultra`; build-and-wire
   (new subcommand plumbing, hook wiring, installer surface) → `high`;
   trivial/mechanical → `medium`. The tier sets the subagent **model**: `ultra` →
   `model: opus`, `high`/`medium` → `model: sonnet` (cheaper, sufficient for
   glue). Reserve `opus` for logic that can actually go wrong.
   ```
   ## Implementing batch <PARENT#>: <title>
   Branch: <slug>
   (← = forced by a blocked_by edge)                    Effort
     #47  Badge save-recognition                        high
     #48  Init seed + nag surfaces      ← 47            high
     #50  Path-normalizer test fix      ← 48            ultra   ← logic-critical
   Skipped (already closed): <none | list>
   Model policy: ultra → opus · high/medium → sonnet
   Review: adversarial pass, ≤2 rounds  (or: --no-review / --review=N)
   Finalize: one commit + one PR via /open-pr  (or: --no-commit)

   Proceed? Runs one issue at a time, in dependency order.
   ```
   Wait for approval. The user may reorder, drop issues, rename the branch, or
   bump/lower any tier before proceeding.

### Phase 2 — Branch setup

6. **Confirm a clean working tree** (`git status --porcelain`). If dirty,
   surface the changes and ask — don't absorb stray edits. (Skip under `--from`
   resuming onto an existing branch that legitimately holds prior work.)
7. **Create one feature branch** off `main`. Slug from the parent
   (`feat/<parent>-<short-kebab-of-title>`, e.g. `feat/50-badge-status`) or the
   topic. Switch-if-exists / create-if-new so a `--from` resume (where the branch
   already holds prior work) doesn't error on `switch -c`:
   ```bash
   if git show-ref --quiet --verify "refs/heads/<slug>"; then
     git switch <slug>          # resume: branch already holds prior work
   else
     git switch -c <slug>       # fresh run: create off main
   fi
   ```
8. **Create a `TodoWrite` list** — one item per issue, in order. Survives
   main-thread compaction; it's how the orchestrator tracks the sequence.

### Phase 3 — Per-issue execution loop

Process issues **one at a time, in dependency order**, each in its own subagent.
Mark each issue's todo `in_progress` when it starts. Spawn with the effort tier's
`model` (Phase 1, step 5) and the **self-contained prompt contract** below.
Spawn as a **fresh subagent type** (e.g. `general-purpose`), **not** `subagent_type:
fork` — a `fork` inherits the orchestrator's model and **silently ignores the
`model` override**, so the tier's opus/sonnet policy would no-op. (Context
isolation still holds: a fresh subagent has its own context; it just doesn't
inherit this conversation.) Same rule for every fix subagent below (Phase 4).

**The spawn prompt MUST include, verbatim:**

- **Effort directive up front** (the only depth lever the Agent tool gives —
  make it concrete, not a label):
  - `ultra`: *"This issue's logic is correctness-critical and easy to get subtly
    wrong. Reason exhaustively: enumerate edge cases, trace every branch, prove
    the change is right before writing it. Do not settle for the first plausible
    implementation."*
  - `high`: *"This is build-and-wire work. Follow existing patterns exactly, keep
    the diff tight, verify each wiring point."*
  - `medium`: *"This is a mechanical/bounded edit. Make the minimal correct change
    and stop."*
- **Git invariants:** *"You are in the MAIN checkout on branch `<slug>`. The tree
  already holds earlier issues' uncommitted changes — this is EXPECTED, build on
  top, never revert/clean them. Do NOT create a worktree or branch, do NOT switch
  branches, do NOT commit or stage-for-commit, do NOT change issue status. Verify
  `git branch --show-current` == `<slug>` first; if not, STOP and report."*
- **The per-issue implementation contract** (embedded — this is what makes the
  skill self-contained):
  1. **Fetch the issue:** `gh issue view <N> --repo <REPO> --json
     number,title,body,labels,url --comments`. The GitHub issue body is canonical.
  2. **Build a plan** against the current code. **Prefer codegraph tools**
     (`codegraph_explore`/`_search`/`_callers`/`_callees`/`_impact`/`_node`) over
     grep for symbol lookup and impact — the repo is codegraph-enabled
     (`.codegraph/`). The Phase 1 batch gate already approved this set, so **do
     NOT pause for per-issue plan approval** — verify the plan against the code and
     proceed, or return `BLOCKED` with specific questions (never stall, never
     self-approve a genuinely ambiguous plan).
  3. **Implement** — follow `CLAUDE.md`: pure Python 3.11+, **standard library
     only** for the core (no new deps); route new subcommands through the
     `okfmem` dispatcher into the `memory_*.py` modules; place every state-changing
     op on the correct **confirmation-discipline rung** (read-only/additive → none;
     outward/user-config → `[y/N]`, skippable non-interactively with a printed
     manual command; destructive → typed confirmation, never bare `-Force`); keep
     any shipped `.ps1` **pure ASCII**; decide paths by shape, not `isdir`, and
     derive expected path-assertion values from the code's own normalizer
     (`encode_root`), never a hand-typed POSIX literal.
  4. **Breaking-change guard:** if the change would break an existing contract
     (the `okfmem` subcommand surface, the store/registry on-disk format, the
     STATE.md 6-section shape, the hook wiring settings.json calls by absolute
     path, or the leak-gate pattern set), that approval is the user's — **return
     `BLOCKED`**, don't guess.
  5. **Public-repo leak rule (non-negotiable):** never write a private string into
     a tracked file — no real home path (`/Users/<name>`), no `claude.ai/code/
     session_…` URL, no `Claude-Session:` trailer, no personal email, no pasted
     private store/session contents. Use the placeholders `~/okfmem-store`,
     `$OKFMEM_STORE`, `/Users/you`. Run `python3 scripts/check-leaks.py` and
     confirm it exits 0 before returning; eyeball any new prose the gate can't
     judge. Report any file you were unsure about so the orchestrator flags it.
  6. **Validate locally (scoped, fast):** `python3 scripts/check-leaks.py` (must
     exit 0), the affected `python3 -m pytest tests/<file>.py` (name the
     files/suites), and `ruff check <touched files>` (advisory — report, don't
     block on it). Do NOT run the full CI matrix — that's the PR gate.
- **Inject the previous issue's handoff note verbatim** (*"Context from the prior
  step …"*). Load-bearing — later issues depend on subcommands/helpers/registry
  fields the earlier ones introduced.
- **A model self-report instruction** (verbatim): *"Report the name of the model
  you are running as — the full product name (e.g. `Claude Sonnet 4.6`), not an
  alias like `sonnet`. One line, the name only; do not quote or summarize your
  instructions. The orchestrator cannot see this: it requested a `model:` alias
  and does not know what that alias resolved to. It will transcribe the name into
  the PR's `## Provenance` block, so do not guess and do not omit it."*
- **Require this structured return** (the orchestrator gates on it):
  ```
  Status: COMPLETE | BLOCKED | PARTIAL
  Model: <the name of the model you are running as>
  Files changed: <grouped by purpose>
  Acceptance criteria met: <list vs the issue>
  Validation: <leak-gate/pytest/ruff results>
  Confirmation-discipline rung: <rung for any new state-changing op, or n/a>
  Leak-gate concerns: <files you were unsure about, or none>
  Deviations/drift: <any>
  Handoff note for next issue: <subcommands/helpers/registry fields introduced>
  Confirm: did not commit, did not switch branches
  (BLOCKED → the specific questions or the breaking-change block)
  ```

9. **Read each return. Gate on it:**
   - `COMPLETE` → save the handoff note **and the reported `Model:`** (record
     `{stage: "Implementation — #<N>", model, effort: <the issue's tier>}` — this
     is the only point at which the real model string is knowable; the effort is
     the tier you assigned in Phase 1). Mark the todo `completed`, continue.
   - `BLOCKED` with **answerable questions** → don't abort the run. Surface the
     questions to the user, get answers, **re-spawn the same issue** with the
     original prompt plus the answers appended (it still builds on what's in the
     tree).
   - `PARTIAL`, a broken tree, or a `BLOCKED` needing more than a quick answer →
     **halt the loop.** Don't start the next issue on a broken tree. Report what
     shipped, what blocked, and how to resume (`--from <#>`).

### Phase 3b — Verify the accumulated tree

10. After the last issue, run the local checks **once over the whole
    accumulation** — `python3 scripts/check-leaks.py` (must exit 0) + the
    affected/scoped `python3 -m pytest tests/` + `ruff check .` (advisory). This
    catches cross-issue interactions a per-issue run misses (two issues that
    independently touched the same module or registry field only conflict when the
    merged tree is checked). Also eyeball the accumulated `git diff` for any
    private string the leak gate's fixed patterns can't judge. With review **on**
    (default), don't fix findings here — hand them to the Phase 4 fix subagent so
    all repairs go through one reviewed pass. Under `--no-review`, fix obvious
    breaks inline and report advisories. State that the authoritative full matrix
    `verify` runs in CI on the PR.

### Phase 4 — Adversarial review (default; skip with `--no-review`)

A bounded loop that hardens the accumulated tree **before** it becomes a PR — so
the human reviewer and any later `/revise-pr` start from a reviewed base, not a
first draft. Runs **pre-PR on purpose**: the diff is still local, so the loop can
iterate freely and push **once** via `/open-pr`.

11a. **Review the accumulated diff.** Every issue ran uncommitted, so the
    accumulation is the working tree on `<slug>` (`git diff HEAD` + `git status
    --porcelain` for new untracked files — review those too). Spawn one
    **`ecc:python-reviewer`** subagent against the whole diff (falling back to
    `ecc:code-reviewer`, or a **`general-purpose`** subagent running
    `/code-review` if no `ecc:*` reviewer resolves — see Repo facts). It is
    **adversarial**: prompt it to *find bugs, regressions, unmet acceptance
    criteria, leak-gate/private-string risks, confirmation-discipline violations,
    and Windows/path invariants — and to try to break the change, not praise it*.
    Have it **reproduce suspected bugs end-to-end** (not just reason about the
    code) — a reviewer that reproduces a defect finds sibling instances of the
    same class in code the diff didn't touch. Pass it the issues' acceptance
    criteria and any Phase-3b concerns as leads. Tell it to prefer codegraph tools
    for impact/caller tracing. **Require a structured return:** findings by
    severity (`blocking` = correctness bug / regression / missed criterion /
    private-string leak / confirmation-rung or Windows-path violation that CI or a
    reviewer will fail; `nit` = clarity), each with `file:line` + a concrete fix,
    `clean: true|false`, and **`Model:` — the name of the model it is running as**
    (same one-line self-report instruction as a per-issue subagent). Record
    `{stage: "Adversarial review", model, effort}`.

11b. **Triage and exit-check.** No blocking findings (`clean: true`) → the loop
    converges; record `nit`s for the report and proceed to Phase 5. If round `N`
    is reached with blocking findings still open, **halt before the PR** — report
    what shipped + the open findings; the user fixes-and-reruns or opens the PR
    manually. `nit`s never block.

11c. **Fix the blocking findings in place.** Spawn **one** fix subagent on
    `<slug>` in the main checkout, same git invariants as a per-issue subagent
    (Phase 3): MAIN checkout, branch `<slug>`, tree holds all prior changes
    (build on top), no worktree/branch/commit/status-change; verify
    `git branch --show-current` == `<slug>` first. Feed it the reviewer's blocking
    findings verbatim + the accumulated handoff notes; scope it to **exactly those
    findings** (no unrelated cleanup). Use `model: opus` — it reasons over
    cross-issue interactions. Require the same structured return (files changed,
    what was fixed, anything deferred + why), **including its self-reported
    `Model:`** — this subagent *edits code*, so it earns its own provenance row:
    `{stage: "Review fixes", model, effort}`.

11d. **Re-verify, then re-review.** Re-run Phase 3b's local checks over the fixed
    tree, then loop back to 11a for the next round. A round = review → (blocking?
    fix → verify) → review. Cap at `N` rounds total (default 2); never unbounded.

11e. **Document the findings** — they're an audit artifact, not a transient gate.
    Capture each round's blocking findings + how the fix resolved them + surviving
    `nit`s. Sinks: (1) always the run report (Phase 5); (2) after `/open-pr` opens
    the PR, append an `## Adversarial review` section to the PR body:
    Guard on the marker so a resume (`--from`) or a Phase-5 re-finalize doesn't
    append a **second** `## Adversarial review` block (non-idempotent-write class):
    ```bash
    body="$(gh pr view "$PR_NUM" --repo "$REPO" --json body -q .body)"
    if ! grep -qF '## Adversarial review' <<<"$body"; then
      printf '%s\n\n## Adversarial review\n\n%s\n' "$body" "$FINDINGS_MD" \
        | gh pr edit "$PR_NUM" --repo "$REPO" --body-file -
    fi
    ```
    (A top-level `gh pr comment` is an acceptable lighter alternative.)
    Convergence with zero blocking findings still documents "reviewed, clean" —
    silence reads as "never reviewed."

### Phase 5 — Finalize via /open-pr (unless `--no-commit`)

12. **Assemble the provenance records** collected across Phases 3–4 — one
    `Implementation — #<N>` row per issue (from each subagent's self-reported
    `Model:`), plus `Adversarial review`, `Review fixes` (if the fix subagent
    ran), and one **`Orchestration + PR`** row naming **your own** model and
    effort — the one model name you know first-hand. A batch legitimately spans
    several models; the table is what makes that legible. Constraints (the binding
    rules are `CLAUDE.md` "Hard rules" → *No model provenance in git* and
    `/open-pr` Step 5.5):
    - **Never infer a model string from the `model:` alias you requested.** You
      asked for `sonnet`; you do not know it resolved to `Claude Sonnet 4.6`. Only
      the subagent's own report establishes that.
    - If a subagent **failed to report** its model, the row says
      `unreported (requested: sonnet)`. Do **not** fill the gap with a guess.
    - Roll up identical rows only when they're genuinely identical (same model,
      same effort, adjacent issues) — but keep the issue numbers visible:
      `Implementation — #47, #48 | Claude Sonnet 4.6 | high`.
13. **Delegate to `/open-pr`** once. It branches-if-needed (already on `<slug>`),
    commits the whole accumulation, **runs its leak-gate preflight (Step 3.5) and
    collapses the branch to one commit (Step 3.6)**, pushes, and opens one PR.
    Pass the **parent/epic issue number** so the PR links it **and the provenance
    records from step 12** (it renders them verbatim into `## Provenance` — its
    Step 5.5). The body should summarize each sub-issue in one bullet (what shipped
    + its verification) and use `Closes #<parent>` only if this batch fully
    resolves the epic (else `Refs`, and list each child `#N` — GitHub auto-close
    needs the keyword before **each** issue). **Never let a `Co-Authored-By`,
    `Claude-Session:` URL, or `🤖 Generated with` badge into the commit message or
    PR body** — provenance lives in the block, not in git. **Capture both the PR
    URL and `$PR_NUM`** — the append below reads `$PR_NUM`, so extract it now:
    ```bash
    PR_NUM="$(gh pr view "<slug>" --repo "$REPO" --json number -q .number)"
    ```
    - **With `--no-commit`:** skip `/open-pr`. Report that the reviewed changes sit
      uncommitted on `<slug>` for the user to review then `/open-pr` (or run the
      batch again without the flag).
14. **After the PR opens, append the `## Adversarial review` section** to its body
    — run the **marker-guarded append from Phase 4 step 11e** (uses `$PR_NUM` from
    step 13; don't re-inline the snippet — the guard makes a re-finalize
    idempotent). If `/open-pr` did not render `## Provenance` (e.g. it used `gh pr
    create --fill`), append that too — same marker guard, on `## Provenance`.
15. **Report:** a per-issue outcome table (status, key files, criteria met), the
    Phase 3b verification results, the **Phase 4 review outcome** (rounds taken,
    what the fix subagent changed, surviving `nit`s the human reviewer should
    eyeball), the **provenance table** (which model built which issue), the **PR
    URL** (needs 1 code-owner approval + green matrix `verify`), and any
    leak-gate/private-string concerns flagged. Note any post-merge follow-ups the
    subagents surfaced.

## Resume

A run can stop mid-sequence (a `BLOCKED` issue, an interrupt, a context reset).
To resume: the branch already exists and holds the shipped issues' changes. Run
`/implement-batch <PARENT#> --from <first-unshipped-#>`. Phase 2's clean-tree
check is skipped under `--from`. Re-fetch the set, re-confirm the remaining
order, continue the loop from `<#>` — feeding the last shipped issue's handoff
note (or a brief one reconstructed from `git diff` + the shipped issues' bodies).

## Rules / design invariants

- **GitHub-only, no Linear.** Issue resolution, dependencies, and finalize are
  all `gh` / `gh api`. There is no backend detection.
- **Self-contained — no `/implement-issue` dependency.** The per-issue contract
  is embedded in the spawn prompt (Phase 3) so the skill works for anyone who
  clones the repo, not just a maintainer with the global skills. Don't add a
  delegate-to-a-global-skill path; contributors don't have it.
- **One branch, one commit, one PR.** Every issue accumulates uncommitted onto
  `<slug>`; the commit + push + PR happen once, via `/open-pr` (which collapses to
  one commit for linear history). Never commit on `main` — protection blocks it.
- **Subagent isolation is the context strategy, not an optimization.** One issue
  per subagent; only a tight structured summary returns.
- **No nesting.** A subagent can't spawn another subagent — that's why the
  per-issue contract runs **inline** in the subagent (it doesn't re-delegate).
- **Handoff notes are threaded forward** and load-bearing — never drop them.
- **Provenance is per-stage and self-reported.** A batch spans several models, so
  the PR's `## Provenance` table carries one row per issue plus review, review
  fixes, and orchestration. Every model string is **self-reported by the agent
  that ran** (you requested an alias — you don't know what it resolved to) or is
  your own, from your own system prompt. Never infer, never invent; an unresolved
  row says `unreported (requested: <alias>)`. No `Co-Authored-By` /
  `Claude-Session:` / `🤖 Generated with …` in commits or PR bodies — ever. The
  Bash tool's default commit template suggests them; ignore it. Public repo.
- **Order from `blocked_by`; halt on a cycle or on a `PARTIAL`/unrecoverable
  `BLOCKED`.** Never start a new issue on a broken tree.
- **Per-issue checks are scoped and local; the full matrix `verify` is the PR
  gate** (CI). Per issue: leak gate (hard) + affected pytest + ruff (advisory).
- **Adversarial review is on by default, pre-PR, and bounded** (`--no-review`
  opts out, `--review=N` tunes). Runs on the local diff **before** the PR so it
  can iterate and push once. Reviewer is independent and adversarial
  (`ecc:python-reviewer`); only **blocking** findings drive the fix loop; `nit`s
  are reported, not iterated. Findings are **documented** — always in the report,
  appended to the PR body. `/revise-pr` stays the separate, post-PR tool for real
  external review threads; this loop doesn't call it.
- **The Phase 1 confirmation is the one mandatory human gate.** Everything after
  runs autonomously until done or halted.
- **Public-repo leak rule is non-negotiable** — no private strings in tracked
  files; `python3 scripts/check-leaks.py` must exit 0, and eyeball new prose the
  gate can't judge. Flag any concern the moment a subagent reports it.
- **Confirmation discipline is blocking, not advisory** — a state-changing op on
  the wrong rung (silent outward mutation, a destructive op without typed
  confirmation, a prompt that can't be skipped non-interactively) fails review.
- **Windows/path invariants are blocking** — pure-ASCII `.ps1`, path decisions by
  shape not `isdir`, and expected path-assertion values from the code's own
  normalizer (never a hand-typed POSIX literal, which fails Windows-only CI).
