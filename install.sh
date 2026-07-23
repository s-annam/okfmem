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

# 3. Wire it up
echo "=> Running backfill and initialization..."
python3 "$ENGINE_DIR/memory_backfill.py"
python3 "$ENGINE_DIR/memory_init.py"

echo ""
echo "✅ okfmem installation complete!"
echo ""
echo "Next steps:"
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo "1. Add ~/.local/bin to your PATH in ~/.bashrc or ~/.zshrc:"
    echo "   export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
echo "2. Check system status by running: okfmem status"
echo "3. (Optional) Set up a remote for your store: git -C $STORE_DIR remote add origin <url>"
echo "4. Check README.md for instructions on wiring up the Stop hook."
