#!/bin/bash
# okfmem — statusline badge for Claude Code.
# Reads the save-state flag the Stop hook writes and renders a compact badge,
# mirroring git's dirty-marker convention (color + trailing `*` carry the state):
#   okfmem*  amber  — work this session not yet captured (run /okfmem-save)
#   okfmem   green  — captured this session
#   (nothing)       — no work, or opted out (OKFMEM_NO_STATUS=1)
#
# Wire it into your statusline command, e.g. prepend to line 1:
#   badge=$(bash /path/to/okfmem-statusline.sh)
# or run it standalone as the whole statusline.
#
# Keystroke-cheap by design: no python, no git, one small file read.

FLAG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.okfmem-status"

# Refuse a symlink — a local attacker could point the flag at a sensitive file
# and have the statusline render its bytes (incl. ANSI escapes) every keystroke.
[ -L "$FLAG" ] && exit 0
[ ! -f "$FLAG" ] && exit 0

# Hard-cap the read and strip anything outside [a-z]; blocks escape-injection
# and OSC hyperlink spoofing via the flag contents.
STATE=$(head -c 16 "$FLAG" 2>/dev/null | tr -d '\n\r' | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z')

# Whitelist. Anything else -> render nothing rather than echo attacker bytes.
case "$STATE" in
  unsaved) printf '\033[38;5;172mokfmem*\033[0m' ;;
  saved)   printf '\033[2;38;5;35mokfmem\033[0m' ;;
  *) exit 0 ;;
esac
