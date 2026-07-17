#!/usr/bin/env bash
# Memory-curate inventory pass.
#
# Usage:  bash inventory.sh <memory-dir>
#
# Emits a markdown report on stdout with three sections:
#   1. Summary  — file count, total bytes, MEMORY.md size, orphan count
#   2. Files    — per-file table with size, age, frontmatter type/name, link status
#   3. Flags    — per-file heuristic signals (ck_snapshot, landed_doc, ...)
#
# Deterministic only: this script flags candidates; the LLM decides verdicts.

set -euo pipefail

MEM_DIR="${1:-}"
if [ -z "$MEM_DIR" ] || [ ! -d "$MEM_DIR" ]; then
  echo "Usage: $0 <memory-dir>" >&2
  echo "Memory dir not found: ${MEM_DIR:-<unset>}" >&2
  exit 1
fi

cd "$MEM_DIR"

# Today as days-since-epoch on macOS (no GNU date -d).
today_epoch_days=$(( $(date +%s) / 86400 ))

# Files in the dir, excluding MEMORY.md.
mapfile -t FILES < <(ls -1 *.md 2>/dev/null | grep -v '^MEMORY\.md$' | sort)

# MEMORY.md links: extract filenames from "](foo.md)" patterns.
LINKED=$(grep -oE '\]\([A-Za-z][A-Za-z0-9_-]*\.md\)' MEMORY.md 2>/dev/null \
  | sed 's/^](//; s/)$//' | sort -u || true)

# --- Summary ---
total_bytes=0
for f in "${FILES[@]}"; do
  total_bytes=$(( total_bytes + $(wc -c < "$f") ))
done
mem_bytes=$(wc -c < MEMORY.md 2>/dev/null || echo 0)
mem_lines=$(wc -l < MEMORY.md 2>/dev/null || echo 0)

# Orphan/dangling counts.
orphan_count=0
dangling_count=0
for f in "${FILES[@]}"; do
  echo "$LINKED" | grep -qx "$f" || orphan_count=$(( orphan_count + 1 ))
done
while IFS= read -r link; do
  [ -z "$link" ] && continue
  [ -f "$link" ] || dangling_count=$(( dangling_count + 1 ))
done <<< "$LINKED"

cat <<EOF
# Memory inventory: $MEM_DIR

## Summary

- **Files** (excl. MEMORY.md): ${#FILES[@]}
- **Total bytes**: $total_bytes
- **MEMORY.md**: $mem_bytes bytes, $mem_lines lines
- **Orphans** (file exists, not linked): $orphan_count
- **Dangling** (linked, file missing): $dangling_count

## Files

| File | Size | Age (d) | Type | Name | Status |
|------|------|---------|------|------|--------|
EOF

# Helper: extract a frontmatter field from a file. Looks between leading --- markers.
fm_field() {
  local file="$1" key="$2"
  awk -v k="$key" '
    BEGIN { in_fm=0 }
    /^---$/ { if (in_fm==0) { in_fm=1; next } else { exit } }
    in_fm==1 {
      if (match($0, "^" k ":[[:space:]]*")) {
        v = substr($0, RLENGTH+1)
        gsub(/^"|"$/, "", v)
        gsub(/^'\''|'\''$/, "", v)
        gsub(/\|/, "/", v)
        print v
        exit
      }
    }
  ' "$file" 2>/dev/null
}

for f in "${FILES[@]}"; do
  size=$(wc -c < "$f")
  mtime_epoch=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null)
  mtime_days=$(( mtime_epoch / 86400 ))
  age=$(( today_epoch_days - mtime_days ))
  type=$(fm_field "$f" type)
  name=$(fm_field "$f" name)
  # Truncate name for table.
  [ ${#name} -gt 50 ] && name="${name:0:47}..."
  status=""
  if echo "$LINKED" | grep -qx "$f"; then
    status="linked"
  else
    status="ORPHAN"
  fi
  printf "| %s | %d | %d | %s | %s | %s |\n" "$f" "$size" "$age" "${type:-?}" "${name:-?}" "$status"
done

# Dangling links section.
if [ "$dangling_count" -gt 0 ]; then
  echo
  echo "### Dangling links (in MEMORY.md but file missing)"
  echo
  while IFS= read -r link; do
    [ -z "$link" ] && continue
    [ -f "$link" ] || echo "- $link"
  done <<< "$LINKED"
fi

# --- Flags section ---
cat <<EOF

## Heuristic flags

| File | Flags |
|------|-------|
EOF

for f in "${FILES[@]}"; do
  flags=()

  # ck_snapshot: ck_YYYY-MM-DD_*.md
  case "$f" in
    ck_[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_*.md) flags+=("ck_snapshot") ;;
  esac

  # landed_doc: filename or frontmatter name has 'landed' or 'operational'
  if echo "$f" | grep -qiE '(landed|operational)' \
     || fm_field "$f" name | grep -qiE '(landed|operational)'; then
    flags+=("landed_doc")
  fi

  # superseded_marker: body grep
  if grep -qE '(SUPERSEDED|superseded by|replaced by|\(Note:)' "$f" 2>/dev/null; then
    flags+=("superseded_marker")
  fi

  # orphan
  if ! echo "$LINKED" | grep -qx "$f"; then
    flags+=("orphan")
  fi

  # age flags
  mtime_epoch=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null)
  mtime_days=$(( mtime_epoch / 86400 ))
  age=$(( today_epoch_days - mtime_days ))
  if [ "$age" -gt 90 ]; then
    flags+=("old_90d")
  elif [ "$age" -gt 45 ]; then
    flags+=("old_45d")
  fi

  if [ ${#flags[@]} -gt 0 ]; then
    IFS=, ; printf "| %s | %s |\n" "$f" "${flags[*]}"
    unset IFS
  fi
done

echo
echo "_Flags are signals, not verdicts. The LLM judgment pass turns these into keep/delete/compress/unsure recommendations._"
