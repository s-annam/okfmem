---
name: revise-pr
description: Revise a pull request against okfmem in response to review — check out the PR branch, fix what each unresolved thread asks for (or reply explaining why not), run the leak gate + checks, commit + push to the PR branch, then reply to and resolve the threads on GitHub. Use when the user says "revise the PR", "/revise-pr", "address the review comments", "fix the review feedback", "clear the comments so it's ready to merge", or a reviewer left changes to act on.
---

# Revise PR

Take a PR from "reviewer left feedback" to "all threads answered and the branch
updated": check out the PR branch → address each **unresolved** review thread in
code (or reply with a rationale when it's out of scope) → run the leak gate +
checks → commit + push to the **PR branch** → reply to each thread and resolve
the ones you actually fixed.

This is the mirror of `open-pr`: that skill gets a change *to* review; this one
closes the loop *after* review.

## Input

Parse the argument for a **PR number** (`12`, `#12`) and optionally
`--repo owner/repo`. If no PR number is given, infer it from the current branch:

```bash
gh pr view --json number,headRefName,state -q '{n:.number,head:.headRefName,state:.state}'
```

If that finds no PR for the current branch and none was passed, list open PRs and
ask which one. Never guess.

## Why this skill exists

`main` is protected with **dismiss-stale-reviews-on-push**: pushing new commits
to a PR branch *dismisses any existing approval*. So the order matters — address
everything in one pass, push once, then re-request review. This skill encodes
that order so a contributor doesn't push piecemeal and burn approvals, and so
every thread gets a visible reply (the PR-author signal norm: don't push silent).

(While the repo is solo, the owner can't approve their own PR — GitHub blocks
self-approval — so the owner merges via admin bypass once `verify` is green. The
dismiss-on-push still applies to any Code Owner review a contributor's PR earned.)

## Process

### Step 0: Detect repo + PR

```bash
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"   # s-annam/okfmem
OWNER="${REPO%%/*}"; NAME="${REPO##*/}"
# PR_NUM from the argument, or inferred from the current branch (see Input).
gh pr view "$PR_NUM" --repo "$REPO" \
  --json number,title,state,headRefName,baseRefName -q '{n:.number,t:.title,s:.state,h:.headRefName,b:.baseRefName}'
```

If the PR is not `OPEN`, stop and say so.

### Step 1: Get onto the PR branch (clean tree)

```bash
gh pr checkout "$PR_NUM" --repo "$REPO"
```

The core is pure Python stdlib — there's no install/build step to redo after
checkout. `gh pr checkout` is its own command: do **not** compound it with a
later `git commit` in one `&&` line (if a `block_commit`-style hook is wired, it
evaluates the branch at the *start* of the command, so a compound
`switch && commit` is judged on the pre-switch branch).

### Step 2: Fetch the unresolved review threads

Threads, not flat comments: only GraphQL exposes `isResolved`, the thread node
`id` (needed to resolve), and each comment's `databaseId` (needed to reply).

```bash
gh api graphql -f query='
query($owner:String!,$name:String!,$pr:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$pr){
      reviewThreads(first:100){ nodes{
        id isResolved isOutdated path line
        comments(first:50){ nodes{ databaseId author{login} body } }
      }}
    }
  }
}' -f owner="$OWNER" -f name="$NAME" -F pr="$PR_NUM" \
  --jq '.data.repository.pullRequest.reviewThreads.nodes[]
        | select(.isResolved==false)
        | {threadId:.id, replyTo:.comments.nodes[0].databaseId,
           path, line, isOutdated,
           author:.comments.nodes[0].author.login,
           body:.comments.nodes[0].body}'
```

Work only the threads where `isResolved==false`. For each, note `threadId` (to
resolve) and `replyTo` (the first comment's `databaseId`, to reply).

### Step 3: Address each thread

For every unresolved thread, read the file at `path` (the code may have moved
since the comment), decide, and act:

- **Actionable change requested** → make the fix in code. Keep the diff scoped to
  what the thread asks; don't fold in unrelated cleanup.
- **Question** → answer it. If the answer reveals a real fix, make the fix too.
- **Out of scope / deferred** → don't force it into this PR. Reply explaining why,
  and if it's worth tracking, file a follow-up issue and link it in the reply.
- **Already addressed / outdated** → nothing to change; you'll resolve it in
  Step 6.

Decide change-vs-defer on the merits — when fixing in place is low-risk, prefer
fixing over filing a follow-up. (Reuse before building: grep for an existing
helper in the `memory_*.py` modules before adding a new one.)

Honor the **confirmation discipline** (`CLAUDE.md`): any change that adds a
state-changing op must sit on the right rung — outward/user-config mutation gets a
`[y/N]`, destructive/irreversible gets a typed confirmation, and every prompt is
skippable non-interactively with the manual command printed. If a review thread
asks you to add or move such an op, match that rung; don't ship a silent mutation.

### Step 4: Run the gates

The repo is public, so the **leak gate is the load-bearing, hard gate** — it
scans the content of every tracked file and exits non-zero naming the offending
`file:line`:

```bash
python3 scripts/check-leaks.py   # HARD — must exit 0 before any push
ruff check .                     # advisory
ruff format --check .            # advisory
pytest -q                        # skip-if-absent; must be green if present
okfmem status                    # smoke: store/hook wiring still reports clean
```

**Exit 0 on the leak gate is not a full clean bill of health.** It catches the
fixed patterns (real home path, `claude.ai/code/session_…` URL, `Claude-Session:`
trailer, personal email) but **cannot** judge whether prose is otherwise private
(an internal project name, an unpublished design detail). For any doc/comment this
revision adds or changes, still eyeball it: use `~/okfmem-store`, `$OKFMEM_STORE`,
or `/Users/you` placeholders. If anything looks private, STOP and sanitize before
pushing. This is the same preflight as `open-pr` Step 3.5.

Do **not** push a red branch to clear comments. If a failure is pre-existing and
unrelated to your change, say so in the report rather than silently shipping it.

### Step 5: Commit + push to the PR branch

```bash
git add <explicit paths>     # stage by path; never `git add -A`/`.`
git commit -m "fix(<scope>): address review comments on PR #${PR_NUM}"
git push origin HEAD
```

Match the commit-type prefix conventions from `CONTRIBUTING.md`
(`feat`/`fix`/`chore`/`refactor`/`docs`/`test`). Never `main` — you're on the PR's
head branch after `gh pr checkout`.

**No AI trailers in the commit** — no `Co-Authored-By: Claude …`, no
`Claude-Session:` trailer, no `https://claude.ai/code/session_…` URL, no
`🤖 Generated with …` badge. The Bash tool's default commit template suggests
them; ignore it. This repo is public — a session URL is an account-scoped
identifier with zero value to any reader of the diff. Model provenance goes in
the **PR body only** (Step 5.5). Same rule as `CLAUDE.md` "Hard rules".

> **Note:** this push dismisses any existing approval (dismiss-stale-reviews-on-push).
> That's expected — Step 7 re-requests review.

### Step 5.1: Collapse to a single commit — **final round only**

`main` requires **linear history** and merges through a **queue that derives the
squash message from the branch** — it carries no commit-message field of its own.
A **one-commit PR lands with that commit's message verbatim**; a multi-commit PR
lands with `wip` / `fix lint` / `address review comments on PR #12` concatenated
into `main`'s history forever. See `open-pr` Step 3.6 for the full rationale.

So the PR should reach the queue as one commit. But **do not collapse on every
round.** Gate it:

- **Leaving any thread open** (you pushed back, or deferred to a follow-up
  issue) → the reviewer is coming back for another round. **Keep the fixup commit
  separate.** They need to diff *just your delta*, not re-read the whole change.
- **Every unresolved thread is now addressed** and you're re-requesting a clean
  approval → **collapse.** This is the last round; the branch's single commit is
  what lands in `main`.

When the gate passes:

```bash
BASE="$(gh pr view "$PR_NUM" --repo "$REPO" --json baseRefName -q .baseRefName)"
git fetch origin "$BASE"
git reset --soft "$(git merge-base HEAD "origin/$BASE")"
git commit -F .git/COMMIT_EDITMSG      # the combined message for the whole PR
git push --force-with-lease origin HEAD
```

Rewrite the message to describe **the change as a whole** — the original intent
plus whatever review actually changed about it. Not "implement X" + "address
review": one coherent story of what lands. Drop the review round-trip entirely;
it's process, not change. `--force-with-lease`, never bare `--force`.

Two things this costs, stated plainly:

- **Reviewers lose per-commit history on the branch.** GitHub still posts a
  `force-pushed … Compare` link, so the old→new diff stays reachable — but it's a
  worse experience than a clean fixup commit, which is exactly why this is gated
  to the final round.
- **Existing review threads may go `isOutdated`** once their anchor lines move.
  Do Step 6 (reply + resolve) **after** this push, using the thread IDs captured
  in Step 2 — replying via `in_reply_to` works regardless of outdated state.

### Step 5.5: Update `## Provenance` if a different model did the revision

If the PR body has a `## Provenance` block and **you are not** the model already
credited there, add a row for the work you just did — you know your own model
first-hand:

| Stage | Model | Effort |
|---|---|---|
| Review revisions | Claude Opus 4.8 | medium |

**Update the existing block in place — never append a second one.** Read the
body, edit the block, write it back:

```bash
body="$(gh pr view "$PR_NUM" --repo "$REPO" --json body -q .body)"
# Write the text-replacement logic (awk/python) to define updated_body:
# if it contains '## Provenance', append the new row to that block;
# if it does NOT, leave it alone (don't retrofit provenance for an older PR).
updated_body="..."
gh pr edit "$PR_NUM" --repo "$REPO" --body-file - <<<"$updated_body"
```

**Never guess a model name** — name your own from your system prompt; take a
subagent's from what it reported. Never infer a version from a `model:` alias,
never invent a row (omit it instead).

### Step 6: Reply to each thread, then resolve what you fixed

Reply on the same thread (uses `in_reply_to`, so no inline-line 422 risk):

```bash
gh api "repos/$REPO/pulls/$PR_NUM/comments" \
  -f body="<concise reply: what changed + commit sha, or why deferred + issue link>" \
  -F in_reply_to="$REPLY_TO"
```

Then resolve **only** the threads you actually addressed or that are outdated:

```bash
gh api graphql -f query='
mutation($id:ID!){ resolveReviewThread(input:{threadId:$id}){ thread{ isResolved } } }' \
  -f id="$THREAD_ID"
```

- Reply to **every** unresolved thread — addressed or deferred. Silence isn't an
  option (PR-author signal norm).
- Resolve threads you fixed or that are outdated. **Don't** resolve a thread where
  you pushed back or deferred — leave it open for the reviewer to close, with your
  rationale visible.
- The reply must match what you did: "Fixed in `<sha>`" only if the code changed;
  "Deferred to #N because …" otherwise. No claiming a fix you didn't make.
- If `resolveReviewThread` fails (you may lack write on a fork), that's
  non-blocking — the reply is the load-bearing part. Note it in the report.

### Step 7: Re-request review and report

The push dismissed the prior approval, so ask the reviewers back:

```bash
gh pr edit "$PR_NUM" --repo "$REPO" --add-reviewer <reviewer-login>
```

Then report: each thread and how it was handled (fixed / answered / deferred +
issue), the commit sha pushed, gate status (leak gate hard-pass + ruff/pytest +
`okfmem status` smoke), and that review was re-requested. Link the PR. To merge,
the PR needs a green **`verify`** check **and 1 approving review from a Code
Owner**; while solo the owner merges via admin bypass once `verify` is green.

## Rules

- **Address, push once, then reply.** Don't push commit-by-commit per comment —
  each push dismisses approval. One pass, one push, then close the threads.
- **Collapse to one commit on the final round only (Step 5.1).** `main` requires
  linear history and the merge queue derives the squash message from the branch,
  so a one-commit PR is the only way to control what lands in `main`. But collapse
  *only* when no thread is left open — a mid-review force-push costs the reviewer
  the delta diff they came back for.
- **Leak gate is the hard gate before every push (Step 4).** `python3
  scripts/check-leaks.py` must exit 0, and eyeball any new prose for private
  strings the gate can't judge. ruff/pytest are advisory/skip-if-absent, but never
  push a red branch to clear comments.
- **Reply to every unresolved thread**, even deferred ones. Resolve only the ones
  you actually fixed (or that are outdated); leave pushback/deferred threads open.
- **Replies must round-trip to the code.** "Fixed in `<sha>`" requires a real
  change in that sha; otherwise say what you deferred and why.
- **Never commit/push to `main`.** Always the PR's head branch (you're on it after
  `gh pr checkout`). The owner can admin-bypass to merge, but don't commit to
  `main` directly.
- **No AI trailers in git; `## Provenance` is updated in place, never stacked.** If
  a different model revised the PR, add its row — naming your own model, which you
  know first-hand. Never infer a version string from a `model:` alias, never
  invent a row, and never stack a second `## Provenance` block.
- **Honor the confirmation-discipline ladder (`CLAUDE.md`).** A revision that adds
  a state-changing op writes it to the rung matching its blast radius —
  outward/user-config mutation gets `[y/N]`, destructive gets a typed
  confirmation, every prompt skippable non-interactively.
- **Stage by explicit path** — never `git add -A`/`.`; a parallel worktree may have
  unrelated unstaged work.
- Pure `gh` + `git` + stdlib Python — no external services, no machine-specific
  paths. Works for any contributor with `gh` installed and authed.
```
