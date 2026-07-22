---
name: open-pr
description: Open a pull request against okfmem from your current work — branch if needed, commit, push, and create the PR with a filled body that links the issue. Use when the user says "open a PR", "send a PR", "/open-pr", or has finished a change and wants it reviewed.
---

# Open PR

Take a change from working tree to an **open pull request** against `main`, in
one skill: branch (if needed) → commit → leak-gate → push → create the PR with a
filled body that links the issue.

## Input

Parse the argument for an **issue number** (e.g. `5`, `#5`), a short commit
message, and optionally `--base <ref>` (overrides the `BASE` from Step 0). If the
issue number is absent, recover it from the branch name (`feat/...-issue-5`,
`gh-5`) or a `Closes #N` / `Refs #N` trailer in an existing commit. If still
unknown, open the PR without an issue link and note that in the output — don't
block on it.

If a calling skill hands over **provenance records** (`{stage, model, effort}`),
render them verbatim into the `## Provenance` block (Step 5.5) — don't re-derive.

## Why this skill exists

`main` is protected (server-side branch protection): every change merges through
a PR with a green **`verify`** CI check, **linear history**, and **1 approving
review from a Code Owner** (`.github/CODEOWNERS`). This skill is the fast, correct
path: it always works on a feature branch, never on `main`. (While the repo is
solo, the owner can't approve their own PR — GitHub blocks self-approval — so the
owner merges via admin bypass, `enforce_admins` being off. That bypass goes away
the day a second maintainer exists; then the approval rule binds everyone.)

## Process

### Step 0: Detect repo + base

```bash
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"              # s-annam/okfmem
BASE="$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)"   # main
# If --base <ref> was provided, override BASE here: BASE="<ref>"
```

### Step 1: Get onto a feature branch (never commit on `main`)

```bash
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
```

- If `BRANCH` is `main`:
  - If there are **committed** commits ahead of `origin/$BASE`, move them onto a
    new branch and reset `main`:
    ```bash
    git switch -c feat/<short-slug>
    git branch --force main origin/main
    ```
  - If there are only **uncommitted** changes, just create the branch (the
    changes follow): `git switch -c feat/<short-slug>`.
  - Pick `<short-slug>` from the issue/topic (e.g. `feat/leak-gate-issue-3`).
- If `BRANCH` is already a feature branch: continue.

### Step 2: Commit any uncommitted work

If `git status --porcelain` shows changes, stage and commit on the feature
branch:

```bash
git add -A
git commit -m "<type: concise summary>"   # feat/fix/chore/refactor/docs/test
```

Use a prepared message if present: `git commit -F COMMIT_EDITMSG`. If the tree is
already clean and there are commits ahead of `origin/$BASE`, skip to Step 3.

**No AI trailers in the commit message.** No `Co-Authored-By: Claude …`, no
`Claude-Session:` trailer, no `https://claude.ai/code/session_…` URL, no
`🤖 Generated with …` badge — the Bash tool's default template suggests them;
ignore it. `Co-Authored-By` implies authorship (the human who ran the model is
the author); a session URL is an account-scoped identifier with zero value to a
reader of a public diff. Model provenance *is* useful — it goes in the **PR body
only** (Step 5.5), never in git history. (This is the same rule as `CLAUDE.md`
"Hard rules".)

### Step 3: Confirm there's something to propose

```bash
git log --oneline origin/$BASE..HEAD
```

If empty, there's nothing to PR — say so and stop.

### Step 3.5: Leak-gate preflight (run before pushing)

The repo is public. A private string in a tracked file (a real home path, a
`claude.ai/code/session_…` URL, a `Claude-Session:` trailer, a personal email) is
the one failure mode expensive to walk back. Run the gate — it scans the content
of every tracked file and exits non-zero naming the offending `file:line`:

```bash
python3 scripts/check-leaks.py   # must exit 0
```

**Exit 0 is not a full clean bill of health.** The gate catches the fixed
patterns above; it **cannot** judge whether prose is otherwise private (an
internal project name, an unpublished design detail). So for any doc/comment this
PR adds or changes, still eyeball it: use `~/okfmem-store`, `$OKFMEM_STORE`, or
`/Users/you` placeholders, and never paste real session/store contents into a
tracked file. If anything looks private, STOP and sanitize before pushing.

### Step 3.6: Collapse the branch to a single commit

`main` requires **linear history**, and a focused change reads best as one
commit whose message is the artifact that lands in `main`. Collapse here — before
the PR exists — costs nothing (no approval to dismiss yet):

```bash
git log --oneline "origin/$BASE..HEAD" | wc -l    # >1 → collapse
```

If more than one commit, write the combined message and collapse:

```bash
git reset --soft "$(git merge-base HEAD "origin/$BASE")"
git commit -F .git/COMMIT_EDITMSG      # the combined message you authored
```

The combined message is **written, not concatenated** — it describes the change
as a whole, not the steps that produced it. Drop `wip` / `fix lint` /
`address review` commits — they're process, not change. Same no-AI-trailer rule
as Step 2 (this message lands in `main`, so it matters more).

```
feat(ci): add leak gate + Python CI, protect main for OSS (#3)

The repo is going public, so a private string in a tracked file is the one
unrecoverable failure mode. Add scripts/check-leaks.py as a fail-fast CI gate,
plus advisory ruff + skip-if-absent pytest.

- .github/workflows/ci.yml: leak gate first, then ruff/pytest
- scripts/check-leaks.py: fragment-assembled patterns, no self-flag
- CLAUDE.md: hard rules (no private strings, no AI trailers, internal/ never ships)

Refs #3
```

### Step 4: Push the branch

```bash
git push -u origin "$BRANCH"
```

If Step 3.6 rewrote an already-pushed branch's history, use
`git push --force-with-lease -u origin "$BRANCH"` (never bare `--force`).

### Step 5: Create the PR

```bash
gh pr create --repo "$REPO" --base "$BASE" --head "$BRANCH" \
  --title "<type: concise summary>" \
  --body  "$(cat <<'BODY'
## Summary

<1–3 sentences: what changed and why.>

Closes #<N>   <!-- omit if not fully resolving; use "Refs #<N>" if partial -->

## Test plan

- [ ] `python3 scripts/check-leaks.py` exits 0
- [ ] `ruff check .` reviewed (advisory)
- [ ] Smoke: `okfmem status` / affected subcommand runs clean

## Provenance

Code implementation via: <Model> (<effort>)
Verification: CI `verify` — green
BODY
)"
```

`gh pr create --fill` derives title/body from the commits but won't add a
`## Provenance` block — append one (Step 5.5) if you use it.

### Step 5.5: The `## Provenance` block

Declare **which model did which stage, at what effort** — method disclosure, not
authorship. It makes cross-model review legible and lets a reader calibrate the
diff. If records were passed in, render them verbatim. Multi-issue batch → table
form, one `Implementation — #<N>` row per issue:

```markdown
## Provenance

| Stage | Model | Effort |
|---|---|---|
| Implementation — #3 | Claude Opus 4.8 | high |
| Review | <model> | <effort> |
| Verification | CI `verify` — green | — |
```

For a single-issue PR you implemented yourself, the prose form in Step 5 is
enough — name **your own** model and effort, which you know first-hand.

**Never guess a model name.** You know your own; you do not know what a
subagent's `model:` alias resolved to. If a stage's model can't be resolved,
state what's true (`<model> (high) — run manually`) or omit the row. A missing
row is honest; a fabricated one is worse than none.

If the body already has a `## Provenance` marker (a re-run), **update it in place
— never append a second block.**

### Step 6: Report

Print the PR URL. To merge, the PR needs a green **`verify`** check **and 1
approving review from a Code Owner**. A contributor's PR waits for an owner's
approval. The owner's own PR can't be self-approved, so while solo the owner
merges via admin bypass once `verify` is green. Request reviewers with
`gh pr edit <num> --add-reviewer <user>`.

## Rules

- **Never commit or push to `main`** — always a feature branch + PR. (Owner can
  admin-bypass, but don't, except to unblock CI itself.)
- **No AI trailers in git; a `## Provenance` block in the PR body instead.** Same
  rule as `CLAUDE.md` — `Co-Authored-By: Claude`/`Claude-Session:`/session URL/🤖
  badge belong nowhere in commits or history; the models go in the PR body.
- **Every provenance row is self-reported or first-hand.** Name your own model
  from your system prompt; take a subagent's from what it reported. Never infer a
  version from a `model:` alias, never invent a row.
- **One commit per PR, message hand-written (Step 3.6).** `main` requires linear
  history; the branch's one commit is the artifact that lands. Collapse before
  the PR exists.
- **One PR per issue/topic.** Keep the diff focused.
- **Run the leak gate before every push (Step 3.5).** `python3 scripts/check-leaks.py`
  must exit 0, and eyeball any new prose for private strings the gate can't judge.
- Use `Closes #N` only when the PR fully resolves the issue; else `Refs #N`.
- Match commit-type prefixes from `CONTRIBUTING.md`
  (`feat`/`fix`/`chore`/`refactor`/`docs`/`test`).
