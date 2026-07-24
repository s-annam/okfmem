#!/usr/bin/env bash
set -e

# -1. Redirect native-Windows shells to install.ps1 --------------------------
# This script needs a POSIX-ish environment (symlinks, `uname`, etc.), which
# WSL and Git Bash both provide even though they run "on Windows". Detect the
# genuinely-native case (a cmd.exe/PowerShell-launched bash with no WSL/Git
# Bash underneath) and point at the PowerShell installer instead of limping
# through with missing primitives. Err toward proceeding here: WSL sets
# $WSL_DISTRO_NAME (or has "microsoft" in /proc/version) and Git Bash's
# `uname -s` reports MINGW*/MSYS*/CYGWIN* — both are left alone.
if [ "${OS:-}" = "Windows_NT" ] && ! command -v uname >/dev/null 2>&1; then
    echo "Native Windows shell detected (no uname, no WSL/Git Bash)."
    echo "Please run install.ps1 instead:"
    echo "   powershell -ExecutionPolicy Bypass -File install.ps1"
    exit 1
fi
if [ "${OS:-}" = "Windows_NT" ] && command -v uname >/dev/null 2>&1; then
    UNAME_S="$(uname -s 2>/dev/null || true)"
    IS_WSL=0
    [ -n "${WSL_DISTRO_NAME:-}" ] && IS_WSL=1
    grep -qi microsoft /proc/version 2>/dev/null && IS_WSL=1
    case "$UNAME_S" in
        MINGW*|MSYS*|CYGWIN*) IS_GITBASH=1 ;;
        *) IS_GITBASH=0 ;;
    esac
    if [ "$IS_WSL" -eq 0 ] && [ "$IS_GITBASH" -eq 0 ]; then
        echo "Native Windows shell detected (not WSL, not Git Bash)."
        echo "Please run install.ps1 instead:"
        echo "   powershell -ExecutionPolicy Bypass -File install.ps1"
        exit 1
    fi
fi

# Reject stray arguments -----------------------------------------------------
# This installer takes NO arguments (the store path comes from $OKFMEM_STORE,
# default ~/okfmem-store). Reject anything passed rather than silently ignoring
# it and running the FULL install -- e.g. a mistyped `--dry-run`, or an
# uninstall-style `--store`. Mirrors the [CmdletBinding()] guard in install.ps1.
if [ $# -gt 0 ]; then
    echo "❌ Error: install.sh takes no arguments (got '$1')."
    echo "   The store path comes from \$OKFMEM_STORE (default ~/okfmem-store)."
    echo "   Usage:  ./install.sh"
    exit 2
fi

echo "=> Installing okfmem..."

# 0. Check dependencies
if ! command -v git >/dev/null 2>&1; then
    echo "❌ Error: 'git' is not installed or not in your PATH."
    echo "   okfmem requires git to version-control your memory store."
    echo "   Please install git and try again."
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ Error: 'python3' is not installed or not in your PATH."
    echo "   okfmem uses python3 (standard library only) for its engine."
    echo "   Please install python3 and try again."
    exit 1
fi

# 1. Setup CLI symlink
mkdir -p ~/.local/bin
ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -L ~/.local/bin/okfmem ] || [ -e ~/.local/bin/okfmem ]; then
    rm -f ~/.local/bin/okfmem
fi
ln -s "$ENGINE_DIR/okfmem" ~/.local/bin/okfmem
echo "=> Symlinked okfmem to ~/.local/bin/okfmem"

# 2. Setup Data Store
STORE_DIR="${OKFMEM_STORE:-$HOME/okfmem-store}"
if [ ! -d "$STORE_DIR" ]; then
    echo "=> Creating local data store at $STORE_DIR"
    mkdir -p "$STORE_DIR"
    git -C "$STORE_DIR" init -q
else
    echo "=> Found existing store at $STORE_DIR"
fi

# The engine's backfill/init/status commands all require a projects/ dir under
# the store (they exit non-zero with "no projects/ under ..." otherwise). A
# fresh `git init` store has none, so create it here -- idempotent for an
# existing store.
mkdir -p "$STORE_DIR/projects"

# NOTE (#27): a fresh `git init` store also has no `.gitignore` -- that is
# NOT fixed up here. `memory_init.py --yes` below creates/maintains the
# store's `.gitignore` (managed block: `.okfmem-sync.lock`, `*.db`,
# `__pycache__/`, `*.pyc`, `.DS_Store`) as one of its own steps, unconditionally
# and before this script (or anything else) ever calls `okfmem sync`.

# 3. Wire it up
echo "=> Running backfill and initialization..."
python3 "$ENGINE_DIR/memory_backfill.py"
# init resolves the CURRENT repo for its memory link from the process cwd
# (git rev-parse), so run it FROM the engine clone -- a piped `curl | bash`
# install launched from ~ would otherwise see no git repo and skip the link.
# --yes: running this installer is the user's consent to wire hooks + create
# the skill/memory links, so init applies them without prompting (the outward
# GitHub-remote step below stays gated separately).
( cd "$ENGINE_DIR" && python3 "$ENGINE_DIR/memory_init.py" --yes )

# 4. Optional: private GitHub remote for the store
# Both linking an existing repo and creating a new one are outward-facing, so we
# only act after an explicit "yes" and only on an interactive terminal (skip
# cleanly when piped). Every path prints the exact manual fallback.
manual_remote_hint() {
    echo "   Skipped. Add one later with:"
    echo "     git -C $1 remote add origin <url>"
}

# Link an already-existing remote store and, if the local store is empty, pull
# its content down. This is the case a returning user hits: the store repo lives
# on GitHub already and the fresh local store just needs to adopt it.
link_existing_store_remote() {
    local store="$1" url="$2"
    if git -C "$store" remote get-url origin >/dev/null 2>&1; then
        git -C "$store" remote set-url origin "$url"
    else
        git -C "$store" remote add origin "$url"
    fi
    echo "=> Linked origin -> $url; fetching..."
    if ! git -C "$store" fetch origin; then
        echo "❌ Fetch failed -- remote is linked but nothing was pulled."
        echo "   Check your access, then:  git -C $store pull"
        return 0
    fi
    # Remote's default branch (main/master) -- don't assume.
    local def
    def="$(git -C "$store" remote show origin 2>/dev/null | sed -n 's/.*HEAD branch: //p')"
    [ -n "$def" ] || def="main"
    if git -C "$store" rev-parse HEAD >/dev/null 2>&1; then
        # Local store already has its own commits -- never clobber them. Do set
        # the upstream so `okfmem sync`/status and the uninstaller's
        # unpushed-work checks track origin again (fetch above guarantees
        # origin/$def exists locally).
        local cur
        cur="$(git -C "$store" branch --show-current)"
        [ -n "$cur" ] && git -C "$store" branch --set-upstream-to="origin/$def" "$cur" >/dev/null 2>&1 || true
        echo "=> Remote linked as origin. Your local store already has commits;"
        echo "   reconcile when ready:  git -C $store pull --rebase origin $def"
        return 0
    fi
    # Empty local store: adopt the remote wholesale. -f overwrites the fresh
    # stubs the installer just wrote (registry.json, projects/) -- the remote is
    # authoritative for a store the user is choosing to pull down.
    if git -C "$store" checkout -f -B "$def" "origin/$def" 2>/dev/null; then
        echo "=> Pulled your memory store from $url (branch $def)."
    else
        echo "❌ Couldn't check out origin/$def cleanly (local files in the way)."
        echo "   Remote is linked; finish by hand with:  git -C $store checkout $def"
    fi
    return 0
}

setup_store_remote() {
    local store="$1"
    if git -C "$store" remote get-url origin >/dev/null 2>&1; then
        echo "=> Store already has a remote: $(git -C "$store" remote get-url origin)"
        return 0
    fi
    [ -t 0 ] || return 0   # non-interactive (piped) -> skip cleanly

    # Without gh we can't detect or create a repo, but the user may already have
    # one -- let them paste its URL so a returning user isn't stuck.
    if ! command -v gh >/dev/null 2>&1; then
        printf "Link a GitHub remote for your store now? (you'll paste its URL) [y/N] "
        read -r ans
        case "$ans" in [Yy]*) ;; *) manual_remote_hint "$store"; return 0 ;; esac
        printf "  Remote URL (git@... or https://...): "
        read -r url
        [ -n "$url" ] || { manual_remote_hint "$store"; return 0; }
        link_existing_store_remote "$store" "$url"
        return 0
    fi
    if ! gh auth status >/dev/null 2>&1; then
        echo "❌ 'gh' is installed but not authenticated."
        echo "   Run:  gh auth login  then re-run the installer, or add a remote by hand:"
        echo "     git -C $store remote add origin <url>"
        return 0
    fi

    # gh is present and authed: does an okfmem-store repo already exist? If so,
    # offer to link + pull it rather than trying (and failing) to create a dup.
    local existing
    existing="$(gh repo view okfmem-store --json url --jq .url 2>/dev/null)"
    if [ -n "$existing" ]; then
        printf "Found an existing private GitHub store:\n    %s\n" "$existing"
        printf "Link it and pull your memory into %s now? [Y/n] " "$store"
        read -r ans
        case "$ans" in [Nn]*) manual_remote_hint "$store"; return 0 ;; esac
        link_existing_store_remote "$store" "$existing"
        return 0
    fi

    printf "No GitHub store found. Create a PRIVATE 'okfmem-store' repo now? [y/N] "
    read -r ans
    case "$ans" in [Yy]*) ;; *) manual_remote_hint "$store"; return 0 ;; esac
    echo "=> Creating private repo 'okfmem-store' and wiring it as origin..."
    if ! gh repo create okfmem-store --private --source "$store" --remote origin; then
        echo "❌ 'gh repo create' failed (see message above)."
        echo "   Add your existing repo as a remote by hand:"
        echo "     git -C $store remote add origin <url>"
        return 0
    fi
    # Push only if the store already has a commit to push.
    if git -C "$store" rev-parse HEAD >/dev/null 2>&1; then
        git -C "$store" push -u origin "$(git -C "$store" branch --show-current)"
    fi
    echo "=> Store remote ready."
}
# Offer remote setup whenever the store has NO remote. setup_store_remote
# early-returns if origin already exists, prompts [y/N] otherwise, and skips
# cleanly when non-interactive -- so this is always safe to call. It
# deliberately includes an established store WITH commits: uninstall.sh can
# delink origin (with consent), so uninstall -> install must round-trip and
# offer the way back. A deliberately local-only user just answers N and gets
# the manual hint.
setup_store_remote "$STORE_DIR" || true

# 5. Optional: okfmem save-state badge in the Claude Code statusline. Outward
# (writes settings.json), so gated on an explicit yes and an interactive
# terminal (skip cleanly when piped). memory_init.py sets it only when no
# statusline exists, and prints a compose snippet if you already have one.
offer_statusline_badge() {
    [ -t 0 ] || return 0
    # Probe the CURRENT statusline first (read-only) so the prompt tells the
    # truth about what pressing Y does in THIS user's case: a fresh setup gets
    # the badge set automatically, but a user who already has a custom
    # statusline gets a compose snippet to paste -- wiring never clobbers it.
    # A blank/unknown probe (older engine, no python) falls back to the generic
    # prompt.
    local state
    state="$( cd "$ENGINE_DIR" && python3 "$ENGINE_DIR/memory_init.py" --statusline-state 2>/dev/null )"
    case "$state" in
        okfmem)
            echo "=> okfmem save-state badge already in your statusline."
            return 0 ;;
        no-claude)
            return 0 ;;  # no ~/.claude yet -- nothing to wire against
        custom)
            printf "You already have a Claude Code statusline. Print a snippet to add the okfmem save-state badge to it? [y/N] " ;;
        *)
            printf "Show an okfmem save-state badge in your Claude Code statusline? [y/N] " ;;
    esac
    read -r ans
    case "$ans" in
        [Yy]*) ( cd "$ENGINE_DIR" && python3 "$ENGINE_DIR/memory_init.py" --wire-statusline ) ;;
        *) echo "   Skipped. Add it later with:  okfmem init --wire-statusline" ;;
    esac
}
offer_statusline_badge || true

# 6. Optional: pre-grant Antigravity (agy) access to the store dir. The store
# lives outside any project workspace, so agy prompts on every memory read
# ("outside workspace"). Marking the store TRUST_FOLDER in
# ~/.gemini/trustedFolders.json stops that -- outward config, so gated on an
# explicit yes and an interactive terminal (skip cleanly + print the manual
# command when piped). Only offered when agy is actually installed.
offer_agy_grant() {
    # Probe first (read-only): not-installed -> no agy, skip silently; granted ->
    # already done, note it; ungranted -> offer. Mirrors offer_statusline_badge.
    local state
    state="$( cd "$ENGINE_DIR" && python3 "$ENGINE_DIR/memory_init.py" --agy-grant-state --store "$STORE_DIR" 2>/dev/null )"
    case "$state" in
        not-installed|"")
            return 0 ;;  # agy absent (or old engine) -- nothing to offer
        granted)
            echo "=> agy / Antigravity already has access to $STORE_DIR."
            return 0 ;;
    esac
    if [ ! -t 0 ]; then
        # Non-interactive (piped/CI): take the documented default (SKIP) and
        # print the exact manual command to grant it later.
        echo "=> agy / Antigravity detected. Grant it access to the store later with:"
        echo "     okfmem init --grant-agy"
        return 0
    fi
    printf "Grant agy / Antigravity access permissions for okfmem-store (%s)? [y/N] " "$STORE_DIR"
    read -r ans
    case "$ans" in
        [Yy]*) ( cd "$ENGINE_DIR" && python3 "$ENGINE_DIR/memory_init.py" --grant-agy --store "$STORE_DIR" ) ;;
        *) echo "   Skipped. Grant it later with:  okfmem init --grant-agy" ;;
    esac
}
offer_agy_grant || true

echo ""
echo "✅ okfmem installation complete!"
echo ""
# The one step the installer CANNOT do for you: init resolves the project to
# wire from the process cwd (git rev-parse), so this run only wired the engine
# clone. Every other repo needs its own `okfmem init`. Make that impossible to
# scroll past -- a missed init is a silently memory-less repo, and the failure
# is invisible (the agent just never remembers anything).
echo "======================================================================"
echo "  ONE MORE STEP -- REQUIRED IN EVERY REPO YOU WANT MEMORY FOR"
echo ""
echo "      cd /path/to/your-repo"
echo "      okfmem init"
echo ""
echo "  This install wired only the repo it ran in ($(basename "$ENGINE_DIR"))."
echo "  The memory link is PER-REPO -- repeat those two lines once in each"
echo "  project. Skip it and your agent silently remembers nothing there."
echo "======================================================================"
echo ""
echo "Other next steps:"
n=1
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo "$n. Add ~/.local/bin to your PATH in ~/.bashrc or ~/.zshrc:"
    echo "   export PATH=\"\$HOME/.local/bin:\$PATH\""
    n=$((n + 1))
fi
echo "$n. Check system status by running: okfmem status"
n=$((n + 1))
echo "$n. The consolidation Stop hook was wired into Claude Code automatically"
echo "   (see the 'stop hook' line above -- nothing to paste). For other"
echo "   agents, the hook snippet is in README.md."
