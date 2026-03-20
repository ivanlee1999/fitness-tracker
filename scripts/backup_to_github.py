#!/usr/bin/env python3
"""
Backup fitness.db to a private GitHub repo ivanlee1999/ivan-fitness-data.

Creates the repo if it doesn't exist, then pushes the current database file.

Usage:
    python backup_to_github.py
"""

import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "fitness.db"
REPO = "ivanlee1999/ivan-fitness-data"
REPO_URL = f"https://github.com/{REPO}.git"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command and return result."""
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def ensure_repo_exists():
    """Create the GitHub repo if it doesn't exist."""
    result = run(["gh", "repo", "view", REPO])
    if result.returncode != 0:
        print(f"Creating private repo {REPO}...")
        result = run([
            "gh", "repo", "create", REPO,
            "--private",
            "--description", "Ivan's fitness tracking database backup",
        ])
        if result.returncode != 0:
            print(f"Failed to create repo: {result.stderr}")
            sys.exit(1)
        print(f"Created repo: {REPO}")
    else:
        print(f"Repo {REPO} already exists.")


def backup():
    """Push fitness.db to the backup repo."""
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        sys.exit(1)

    ensure_repo_exists()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_size = DB_PATH.stat().st_size

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Clone or init
        result = run(["gh", "repo", "clone", REPO, str(tmpdir / "repo")], cwd=str(tmpdir))
        repo_dir = tmpdir / "repo"

        if result.returncode != 0:
            # Fresh repo, init it
            repo_dir.mkdir(exist_ok=True)
            run(["git", "init"], cwd=str(repo_dir))
            run(["git", "remote", "add", "origin", REPO_URL], cwd=str(repo_dir))

        # Copy database
        import shutil
        shutil.copy2(DB_PATH, repo_dir / "fitness.db")

        # Write metadata
        (repo_dir / "backup_info.txt").write_text(
            f"Fitness DB Backup\n"
            f"Timestamp: {timestamp}\n"
            f"Size: {db_size:,} bytes\n"
        )

        # Commit and push
        run(["git", "add", "fitness.db", "backup_info.txt"], cwd=str(repo_dir))
        result = run(
            ["git", "commit", "-m", f"Backup {timestamp}"],
            cwd=str(repo_dir),
        )

        if "nothing to commit" in (result.stdout + result.stderr):
            print("No changes to backup.")
            return

        result = run(
            ["git", "push", "-u", "origin", "HEAD:main"],
            cwd=str(repo_dir),
        )
        if result.returncode != 0:
            # Try with --force for first push
            run(
                ["git", "push", "-u", "origin", "HEAD:main", "--force"],
                cwd=str(repo_dir),
            )

    print(f"\nBackup complete: {db_size:,} bytes pushed to {REPO}")


if __name__ == "__main__":
    backup()
