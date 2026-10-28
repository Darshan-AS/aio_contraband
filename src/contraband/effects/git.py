"""Safe Git repository staging, commit, and push adapter."""

import shutil
import subprocess
from pathlib import Path

from contraband.core.errors import GitError
from contraband.core.result import Err, Ok, Result

MANAGED_ADDON_PATHS: tuple[str, ...] = (
    "manifest.json",
    "catalog_index.json",
    "catalog",
    "meta",
    "stream",
)


def _detect_git_branch(git_bin: str, repo_dir: Path) -> str:
    proc = subprocess.run(
        [git_bin, "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    branch = proc.stdout.strip() if proc.returncode == 0 else ""
    return branch if branch and branch != "HEAD" else "main"


def publish_git_repo(
    repo_dir: Path,
    commit_message: str = "feat(catalog): Sync Contraband metadata and stream links",
    *,
    branch: str | None = None,
    push: bool = True,
) -> Result[bool, GitError]:
    """
    Stages managed static Stremio addon paths (`manifest.json`, `catalog_index.json`,
    `catalog/`, `meta/`, `stream/`), commits, and optionally pushes changes in `repo_dir`
    to `origin <branch>`.
    Returns `Ok(True)` if changes were committed, `Ok(False)` if managed paths were clean.
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
            [git_bin, "-C", str(resolved_dir), "status", "--porcelain", "--", *MANAGED_ADDON_PATHS],
            capture_output=True,
            text=True,
            check=False,
        )
        if status_proc.returncode != 0:
            return Err(GitError(f"git status failed: {status_proc.stderr.strip()}"))
        if not status_proc.stdout.strip():
            return Ok(False)

        active_paths = [
            p
            for p in MANAGED_ADDON_PATHS
            if (resolved_dir / p).exists() or p in status_proc.stdout
        ]
        if not active_paths:
            return Ok(False)

        subprocess.run(
            [git_bin, "-C", str(resolved_dir), "add", "-A", "--", *active_paths],
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
        if push:
            target_branch = branch or _detect_git_branch(git_bin, resolved_dir)
            subprocess.run(
                [git_bin, "-C", str(resolved_dir), "push", "origin", target_branch],
                capture_output=True,
                text=True,
                check=True,
            )
        return Ok(True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        return Err(GitError(f"Git publish failed in {resolved_dir.name}: {stderr or exc}"))
