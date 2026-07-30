import subprocess
from pathlib import Path

__version__ = "5.3.0"


def _git_commit() -> str:
    root = Path(__file__).parents[2]
    if not (root / ".git").exists():
        return ""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


__commit__ = _git_commit()
