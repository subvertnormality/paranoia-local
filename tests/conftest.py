import subprocess
from pathlib import Path

import pytest

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}


def git(args: list[str], cwd: Path) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**_GIT_ENV, "HOME": str(cwd)},
    )
    return r.stdout


def commit_all(repo: Path, msg: str) -> None:
    git(["add", "-A"], repo)
    git(["commit", "-q", "-m", msg], repo)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo on `main` with one commit and a couple of files."""
    r = tmp_path / "proj"
    r.mkdir()
    git(["init", "-q", "-b", "main"], r)
    (r / "README.md").write_text("# proj\n")
    (r / "app.py").write_text('"""App module."""\n\n\ndef greet(name):\n    return f"hi {name}"\n')
    commit_all(r, "initial commit")
    return r


@pytest.fixture
def repo_with_branch(repo: Path) -> Path:
    """`repo` plus a `feature` branch that changes app.py, committed."""
    git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "app.py").write_text(
        '"""App module."""\n\n\ndef greet(name):\n    return f"hello {name}!"\n'
    )
    (repo / "extra.py").write_text('"""Extra."""\n\n\ndef add(a, b):\n    return a + b\n')
    commit_all(repo, "feature: friendlier greeting + add helper")
    return repo
