#!/usr/bin/env python3
"""Fail CI when a source backend is added or removed without updating CLAUDE.md.

CLAUDE.md documents the source-backend contract and lists every backend, so
adding or deleting a `basemap_tile_downloader/sources/*.py` module is exactly the
change that silently dates it. This turns "please keep CLAUDE.md current" from a
good intention into a build gate for the one case where staleness is both certain
and easy to miss.

Deliberately narrow. It does NOT fire on edits to an existing backend (far too
noisy — most source edits don't change what CLAUDE.md says, and a gate that cries
wolf gets bypassed). Only the add/remove of a whole backend module trips it. If
CLAUDE.md genuinely needs no change for such a rare event, updating it is still
the right reflex; a truly empty touch is a smell worth the friction.

Compares BASE..HEAD (env vars). Skips cleanly when the range can't be resolved
(new branch, force-push, shallow checkout) rather than failing on infrastructure.
"""
import os
import subprocess  # nosec B404  (fixed git args, no shell, no user input)
import sys

DOCS = "CLAUDE.md"
SOURCES_PREFIX = "basemap_tile_downloader/sources/"
ZERO_SHA = "0" * 40


def _git(*args):
    return subprocess.check_output(["git", *args], text=True)  # nosec B603 B607


def _names(base, head, diff_filter):
    out = _git("diff", f"--diff-filter={diff_filter}", "--name-only",
               base, head)
    return {line.strip() for line in out.splitlines() if line.strip()}


def _is_backend(path):
    return (path.startswith(SOURCES_PREFIX)
            and path.endswith(".py")
            and os.path.basename(path) != "__init__.py")


def main():
    base = (os.environ.get("BASE_SHA") or "").strip()
    head = (os.environ.get("HEAD_SHA") or "").strip() or "HEAD"

    if not base or base == ZERO_SHA:
        print(f"No base commit to compare against; skipping the {DOCS} guard.")
        return 0
    # Both endpoints must exist in this checkout (needs fetch-depth: 0). If not,
    # skip rather than fail — the guard must never block on a CI plumbing quirk.
    try:
        _git("cat-file", "-e", f"{base}^{{commit}}")
        _git("cat-file", "-e", f"{head}^{{commit}}")
    except subprocess.CalledProcessError:
        print(f"Range {base}..{head} not fully present; skipping the {DOCS} guard.")
        return 0

    try:
        added_or_removed = _names(base, head, "AD")   # Added or Deleted files
        changed = _names(base, head, "ACMRD")         # any change, incl. renames
    except subprocess.CalledProcessError as e:
        print(f"git diff failed ({e}); skipping the {DOCS} guard.")
        return 0

    backends = sorted(p for p in added_or_removed if _is_backend(p))
    if backends and DOCS not in changed:
        print(f"ERROR: a source backend was added or removed but {DOCS} was not "
              f"updated in the same change:")
        for p in backends:
            print(f"    {p}")
        print(f"\n{DOCS} lists the backends and documents the source contract, so "
              f"it goes stale exactly here. Update its 'source-backend contract' "
              f"section (the backend list and the three wiring points), then "
              f"re-push. See CLAUDE.md itself for what to record.")
        return 1

    print(f"{DOCS} guard OK "
          f"({len(backends)} backend add/remove(s); "
          f"{DOCS} {'updated' if DOCS in changed else 'unaffected'}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
