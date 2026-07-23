#!/usr/bin/env bash
set -e

# -1. Redirect native-Windows shells to uninstall.ps1 -------------------------
# See install.sh's identical block for the reasoning: this script needs a
# POSIX-ish environment (symlinks, `uname`, etc.), which WSL and Git Bash both
# provide even though they run "on Windows". Detect the genuinely-native case
# (a cmd.exe/PowerShell-launched bash with no WSL/Git Bash underneath) and
# point at the PowerShell uninstaller instead of limping through with missing
# primitives.
if [ "${OS:-}" = "Windows_NT" ] && ! command -v uname >/dev/null 2>&1; then
    echo "Native Windows shell detected (no uname, no WSL/Git Bash)."
    echo "Please run uninstall.ps1 instead:"
    echo "   powershell -ExecutionPolicy Bypass -File uninstall.ps1"
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
        echo "Please run uninstall.ps1 instead:"
        echo "   powershell -ExecutionPolicy Bypass -File uninstall.ps1"
        exit 1
    fi
fi

echo "=> Uninstalling okfmem..."

# 0. Resolve args / paths ----------------------------------------------------
STORE_DIR="${OKFMEM_STORE:-$HOME/okfmem-store}"
DRY_RUN=0
ASSUME_YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --yes|-y)
            ASSUME_YES=1
            shift
            ;;
        --store)
            # A missing or dash-prefixed value means the user fumbled the flag
            # (e.g. `--store --dry-run`). A real store path never starts with a
            # dash -- fail fast rather than silently adopting a flag as a path.
            case "${2:-}" in
                ''|-*)
                    echo "❌ Error: --store needs a path value (got '${2:-}')."
                    echo "   Usage:  ./uninstall.sh [--dry-run] [--store <path>]"
                    exit 2
                    ;;
            esac
            STORE_DIR="$2"
            shift 2
            ;;
        --store=*)
            STORE_DIR="${1#--store=}"
            shift
            ;;
        *)
            # Reject unknown flags instead of silently swallowing them. Without
            # this, a mistyped `--dryrun` / `-n` fell through to `shift` and was
            # dropped -- so DRY_RUN stayed 0 and the uninstaller ran FOR REAL
            # while the user believed they were previewing.
            echo "❌ Error: unknown argument '$1'."
            echo "   Usage:  ./uninstall.sh [--dry-run] [--yes] [--store <path>]"
            exit 2
            ;;
    esac
done

if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ Error: 'python3' is not installed or not in your PATH."
    echo "   okfmem uses python3 (standard library only) for its engine."
    exit 1
fi

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 0.5 Up-front confirmation gate ----------------------------------------------
# ONE [y/N] before anything is removed. "Uninstall" is a destructive-by-name
# operation: the user must get to decline BEFORE step 1, not after. Covers the
# CLI wrapper and the harness wiring (both restored by re-running install.sh).
# The store's git remote and data have their own gates below -- --yes does NOT
# reach the data delete.
if [ "$DRY_RUN" -eq 0 ] && [ "$ASSUME_YES" -eq 0 ]; then
    if [ -t 0 ]; then
        printf "Uninstall okfmem (remove CLI wrapper and harness wiring)? [y/N] "
        read -r ans
        case "$ans" in
            [Yy]*) ;;
            *)
                echo "=> Aborted -- nothing removed."
                exit 0
                ;;
        esac
    else
        echo "=> No interactive prompt available and --yes not given -- aborting, nothing removed."
        echo "   Re-run in an interactive terminal to be asked, or pass --yes to run unattended:"
        echo "     ./uninstall.sh --yes"
        exit 0
    fi
fi

# 1. Remove the CLI wrapper ---------------------------------------------------
if [ -L ~/.local/bin/okfmem ] || [ -e ~/.local/bin/okfmem ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "=> [dry-run] would remove ~/.local/bin/okfmem"
    else
        rm -f ~/.local/bin/okfmem
        echo "=> Removed ~/.local/bin/okfmem"
    fi
else
    echo "=> ~/.local/bin/okfmem not present"
fi

# 2. Strip okfmem-managed harness wiring --------------------------------------
# Covered by the up-front gate above (rung-2 consent already given -- via
# prompt or explicit --yes). Reversible by re-running install.sh, and
# memory_uninstall.py only ever touches wiring okfmem itself created -- it
# skips any real file it didn't write. The store's data and git remote are
# never touched here (gated separately below).
UNINSTALL_PY="$ENGINE_DIR/memory_uninstall.py"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "=> [dry-run] would remove harness wiring (pointers, skill links, hooks):"
    python3 "$UNINSTALL_PY" --dry-run --store "$STORE_DIR"
else
    echo "=> Removing harness wiring (pointers, skill links, hooks)..."
    python3 "$UNINSTALL_PY" --store "$STORE_DIR"
fi

# 3. Delink the store remote (opt-in, rung-2) ---------------------------------
# The store and its full history stay on disk either way -- this only drops
# the `origin` remote pointer. Skip cleanly if there's no remote to delink, or
# if the terminal isn't interactive (piped/CI run).
if git -C "$STORE_DIR" remote get-url origin >/dev/null 2>&1; then
    ORIGIN_URL="$(git -C "$STORE_DIR" remote get-url origin)"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "=> [dry-run] store has a remote ($ORIGIN_URL) -- would offer to delink (default: keep)."
    elif [ -t 0 ]; then
        printf "Store has a remote (%s). Delink it (git remote remove origin)? [y/N] " "$ORIGIN_URL"
        read -r ans
        case "$ans" in
            [Yy]*)
                git -C "$STORE_DIR" remote remove origin
                echo "=> Remote delinked. Store data is untouched."
                ;;
            *)
                echo "   Skipped. Delink later with:"
                echo "     git -C $STORE_DIR remote remove origin"
                ;;
        esac
    else
        echo "=> Store has a remote ($ORIGIN_URL) -- skipped (non-interactive)."
        echo "   Delink later with:  git -C $STORE_DIR remote remove origin"
    fi
else
    echo "=> Store has no remote to delink."
fi

# 4. Full data delete (opt-in, double-guarded, rung-3) ------------------------
# Default is KEEP. Refuses entirely on a non-interactive run. Requires BOTH an
# initial [y/N] AND a typed confirmation (the exact store path, or "DELETE")
# before touching anything -- a bare `-f`/`--yes` cannot trigger this.
if [ ! -d "$STORE_DIR" ]; then
    echo "=> No store found at $STORE_DIR -- nothing to delete."
elif [ "$DRY_RUN" -eq 1 ]; then
    echo "=> [dry-run] would offer to delete all data at $STORE_DIR (double-confirm; default: keep)."
elif [ ! -t 0 ]; then
    echo "=> Store data at $STORE_DIR was NOT deleted (non-interactive run)."
    echo "   Delete it yourself if you're sure:  rm -rf \"$STORE_DIR\""
else
    echo ""
    echo "Store data lives at: $STORE_DIR"

    # Pending-work checks so the user sees what deleting would actually lose.
    # Three independent ways a store can hold data that exists nowhere else:
    #   (a) committed but unpushed  -> rev-list @{u}..HEAD
    #   (b) commits but NO upstream -> nothing is backed up at all
    #   (c) uncommitted / untracked -> dirty working tree (unsaved edits)
    # (a) alone was checked before; (b) and (c) could be lost silently.
    LOSS_WARNING=0
    if git -C "$STORE_DIR" rev-parse --abbrev-ref --symbolic-full-name @{u} >/dev/null 2>&1; then
        UNPUSHED_COUNT="$(git -C "$STORE_DIR" rev-list --count @{u}..HEAD 2>/dev/null || echo 0)"
        if [ "${UNPUSHED_COUNT:-0}" -gt 0 ] 2>/dev/null; then
            echo "⚠️  $UNPUSHED_COUNT commit(s) not yet pushed to the remote."
            LOSS_WARNING=1
        fi
    elif git -C "$STORE_DIR" rev-parse HEAD >/dev/null 2>&1; then
        echo "⚠️  Store has commits but no upstream remote -- nothing is backed up."
        LOSS_WARNING=1
    fi
    if [ -n "$(git -C "$STORE_DIR" status --porcelain 2>/dev/null || true)" ]; then
        echo "⚠️  Store has uncommitted or untracked changes (unsaved memory edits)."
        LOSS_WARNING=1
    fi
    if [ "$LOSS_WARNING" -eq 1 ]; then
        echo "    Deleting now would lose that memory data with no backup."
    fi
    printf "Delete ALL memory data at %s? [y/N] " "$STORE_DIR"
    read -r ans
    case "$ans" in
        [Yy]*)
            printf "Type the store path (or DELETE) to confirm: "
            read -r confirm
            if [ "$confirm" = "$STORE_DIR" ] || [ "$confirm" = "DELETE" ]; then
                rm -rf "$STORE_DIR"
                echo "=> Deleted $STORE_DIR."
            else
                echo "=> Confirmation did not match -- aborted. Data intact."
            fi
            ;;
        *)
            echo "=> Skipped. Data intact at $STORE_DIR."
            ;;
    esac
fi

echo ""
if [ "$DRY_RUN" -eq 1 ]; then
    echo "✅ [dry-run] no changes made. Re-run without --dry-run to apply."
else
    echo "✅ okfmem uninstalled."
fi
echo ""
echo "The engine clone itself ($ENGINE_DIR) is untouched -- delete it by hand"
echo "if you no longer need it."
if [ -d "$STORE_DIR" ]; then
    echo "Your memory data is still at $STORE_DIR."
fi
if [[ ":$PATH:" == *":$HOME/.local/bin:"* ]]; then
    echo "(~/.local/bin is still on your PATH -- harmless now that okfmem is removed.)"
fi
