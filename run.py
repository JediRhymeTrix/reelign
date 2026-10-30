"""Standalone job entry point; no installation/package-path tricks required."""

from syncer.runner import main

if __name__ == "__main__":
    raise SystemExit(main())