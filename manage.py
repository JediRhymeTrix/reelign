"""Run the local setup helper without starting the synchronization runner."""

from syncer.setup import main


if __name__ == "__main__":
    raise SystemExit(main())