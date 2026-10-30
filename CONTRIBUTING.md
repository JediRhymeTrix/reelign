# Contributing to Reelign

Use your own fork for code changes, not your configured deployment repository.
Never commit credentials, personal list configuration, OAuth responses, encrypted
operational state, or production run logs.

```text
python -m venv .venv
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python manage.py validate
python manage.py render-workflows --check
python run.py --validate-only
python -m compileall -q syncer tests scripts
```

Python 3.12+ is required. Activate the virtual environment using your shell's
standard command. Tests are offline: no provider accounts or API keys needed.

Write a failing regression before changing behavior. Preserve typed ID matching,
newest-first source order, rank-based destination order, complete reads before
removals, dry-run purity, legacy state compatibility and idempotency. Test both
Windows and Linux. Use pinned dependencies and SHA-pinned GitHub Actions.

Edit schedules in config, then render them; do not manually change generated
`schedule-*.yml` wrappers. Documentation changes should update examples and pass
internal-link tests. Keep the own-list and public-curator use cases equally clear.

Open a focused PR with rationale, tests, compatibility/backport implications and
sanitized evidence. Report vulnerabilities privately per [SECURITY.md](SECURITY.md).
Detailed guide: [development and contribution](docs/contributing.md).