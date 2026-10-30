"""Copy only shared engine/tools/tests into an existing private job directory."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from syncer.model import SyncError


def shared_files(source):
    files = {}
    for directory in ("syncer", "tests", "scripts"):
        for path in (source / directory).glob("*.py"):
            files[path.relative_to(source).as_posix()] = path
    for name in ("run.py", "manage.py", "requirements.txt"):
        if (source / name).is_file():
            files[name] = source / name
    for path in (source / "tests" / "fixtures").rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            files[path.relative_to(source).as_posix()] = path
    return files


def backport(source, target, check=False):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target == source or source in target.parents or target in source.parents:
        raise SyncError("Backport target must be a separate existing integration directory")
    if not target.is_dir():
        raise SyncError("Backport target directory must already exist")
    files = shared_files(source)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
    try:
        version = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                          text=True, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, OSError):
        version = "unversioned"
    manifest = {"upstream": "https://github.com/JediRhymeTrix/reelign",
                "commit": version, "files": hashes}
    if check:
        for name, digest in hashes.items():
            path = target / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return False
        for directory in ("syncer", "tests", "scripts"):
            if any(path.relative_to(target).as_posix() not in files
                   for path in (target / directory).glob("*.py")):
                return False
        return True
    for name, path in files.items():
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    # Remove only stale shared Python modules, never config, state or other jobs.
    for directory in ("syncer", "tests", "scripts"):
        for path in (target / directory).glob("*.py"):
            if path.relative_to(target).as_posix() not in files:
                path.unlink()
    (target / "UPSTREAM.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        success = backport(Path(__file__).resolve().parents[1], args.target, args.check)
        print("Shared engine parity verified" if success else "Shared engine drift detected")
        return 0 if success else 1
    except SyncError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())