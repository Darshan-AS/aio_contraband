"""Safe Git repository staging, commit, and push adapter."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from contraband.core.errors import GitError
from contraband.core.result import Err, Ok, Result


def publish_git_repo(
    repo_dir: Path,
    commit_message: str = "feat(catalog): Sync Contraband metadata and stream links",
) -> Result[bool, GitError]:
    """
    Stages, commits, and pushes changes in `repo_dir` to `origin main`.
    Returns `Ok(True)` if changes were committed and pushed, `Ok(False)` if the working tree was clean.
    """
    resolved_dir = repo_dir.resolve()
    if not (resolved_dir / ".git").exists():
        return Ok(False)

    git_bin = shutil.which("git")
    if not git_bin:
        return Err(GitError("git binary not found on PATH."))

    clean_msg = commit_message.strip() or "feat(catalog): Sync Contraband metadata and stream links"

    try:
        status_proc = subprocess.run(
            [git_bin, "-C", str(resolved_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if status_proc.returncode != 0:
            return Err(GitError(f"git status failed: {status_proc.stderr.strip()}"))
        if not status_proc.stdout.strip():
            return Ok(False)

        subprocess.run(
            [git_bin, "-C", str(resolved_dir), "add", "-A"],
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            [git_bin, "-C", str(resolved_dir), "commit", "-m", clean_msg],
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            [git_bin, "-C", str(resolved_dir), "push", "origin", "main"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Ok(True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        return Err(GitError(f"Git publish failed in {resolved_dir.name}: {stderr or exc}"))
