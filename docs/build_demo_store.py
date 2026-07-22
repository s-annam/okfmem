#!/usr/bin/env python3
"""Build a deterministic okfmem sandbox store for the demo GIF.

Creates a store with an epoch ~90d ago and a mix of pages so
`okfmem consolidate --dry-run` shows real reinforcement + archival
without touching the user's real ~/okfmem-store.
"""
import json
import os
import subprocess
import sys
from datetime import date, timedelta

STORE = sys.argv[1]
TODAY = date.today()


def d(days_ago):
    return (TODAY - timedelta(days=days_ago)).isoformat()


def page(slug, typ, desc, created, last_accessed, access_count, pinned, body):
    fm = (
        f"---\nname: {slug}\ndescription: {desc}\n"
        f"type: {typ}\ncreated: {created}\nlast_accessed: {last_accessed}\n"
        f"access_count: {access_count}\npinned: {str(pinned).lower()}\n"
        f"importance: 0.5\n---\n\n{body}\n"
    )
    return fm


mem_dir = os.path.join(STORE, "projects", "acme-api", "memory")
os.makedirs(mem_dir, exist_ok=True)

# epoch 90 days ago -> stale pages get past the 30d grace window
with open(os.path.join(STORE, "decay_state.json"), "w") as f:
    json.dump({"epoch": d(90)}, f)
    f.write("\n")

pages = [
    # hot, frequently read -> survives, gets reinforced
    ("jwt-refresh-race", "project", "refresh-token race fixed with a mutex",
     d(120), d(2), 9, False,
     "Concurrent refresh double-rotated the token. Fixed with a per-user "
     "mutex in `auth/refresh.py`."),
    # pinned decision -> never archived
    ("db-is-postgres", "reference", "prod DB is Postgres 15, not MySQL",
     d(200), d(80), 1, True,
     "Prod runs Postgres 15. Do not assume MySQL syntax."),
    # stale, low access -> archive candidate
    ("old-webpack-config", "project", "legacy webpack 4 build quirks",
     d(210), d(85), 0, False,
     "Notes on the webpack 4 build. Superseded by the Vite migration."),
    # stale, low access -> archive candidate
    ("intern-onboarding-2024", "project", "onboarding steps for the 2024 intern",
     d(220), d(88), 0, False,
     "One-off onboarding checklist. No longer relevant."),
]
for args in pages:
    with open(os.path.join(mem_dir, args[0] + ".md"), "w") as f:
        f.write(page(*args))

with open(os.path.join(mem_dir, "MEMORY.md"), "w") as f:
    f.write(
        "# MEMORY — acme-api\n\n"
        "- [jwt-refresh-race](jwt-refresh-race.md) — refresh-token race fixed with a mutex\n"
        "- [db-is-postgres](db-is-postgres.md) — prod DB is Postgres 15, not MySQL (pinned)\n"
        "- [old-webpack-config](old-webpack-config.md) — legacy webpack 4 build quirks\n"
        "- [intern-onboarding-2024](intern-onboarding-2024.md) — onboarding for the 2024 intern\n"
    )

subprocess.run(["git", "-C", STORE, "init", "-q"], check=False)
print(f"sandbox store built at {STORE}")
