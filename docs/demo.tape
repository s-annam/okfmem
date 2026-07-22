# okfmem demo — regenerate with:  vhs docs/demo.tape   (run from the repo root)
#
# Records a sanitized, self-contained walkthrough into docs/demo.gif:
#   the markdown store (MEMORY.md index) -> harnesses wired -> the engine
#   forgetting stale pages while keeping the hot + pinned ones.
# All setup runs against a throwaway $HOME/$OKFMEM_STORE, so recording this
# never touches your real memory store or agent configs.

Output docs/demo.gif

Set Theme "Catppuccin Mocha"
Set FontSize 20
Set Width 1400
Set Height 760
Set Padding 24
Set TypingSpeed 45ms

# --- setup (not recorded) ---
Hide
Type "export PATH=$PWD:$PATH HOME=/tmp/okfmem-demo-home OKFMEM_STORE=/tmp/okfmem-demo-home/okfmem-store" Enter
Type "rm -rf $HOME; mkdir -p $HOME/.claude $HOME/.gemini/config $OKFMEM_STORE" Enter
Type "python3 docs/build_demo_store.py $OKFMEM_STORE >/dev/null" Enter
Type "okfmem init >/dev/null 2>&1" Enter
Type "export PS1='\033[38;5;212m$\033[0m '" Enter
Type "cd $OKFMEM_STORE/projects/acme-api/memory" Enter
Type "clear" Enter
Show

# --- recorded ---
Sleep 600ms
Type "# okfmem — self-maintaining markdown memory for coding agents" Sleep 400ms Enter
Sleep 800ms

# The always-loaded index: one line per topic, capped at 200 lines.
Type "cat MEMORY.md" Sleep 300ms Enter
Sleep 2600ms

# Wired into your agents — no daemon, no database.
Type "okfmem status" Sleep 300ms Enter
Sleep 2800ms

# The engine forgets: stale pages decay to archive, hot + pinned survive.
Type "okfmem consolidate --dry-run" Sleep 300ms Enter
Sleep 4200ms
