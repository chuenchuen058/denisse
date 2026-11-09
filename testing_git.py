"""Hermetic git for test fixtures that build a throwaway repo in a temp folder.

A test that runs git (directly, or through the code it tests) inherits the caller's
environment. A lane session can carry GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE, and then
"git init / config / commit in the temp dir" acts on the REAL repo: on 9 Nov 2026 a fixture
set core.bare=true and a fake [user] in the main checkout's .git/config, and made a branch
and two commits there. Call ``hermetic_git(self)`` first thing in setUp (before any git
call) and every git process for the rest of the test sees only the temp repos:

- every GIT_* variable is dropped,
- system config is off (GIT_CONFIG_NOSYSTEM=1) and the global config is an empty temp file,
  so nothing from the machine (hooksPath, autocrlf, identity, safe.directory) leaks in,
- git never prompts (GIT_TERMINAL_PROMPT=0),

and os.environ is restored when the test finishes (addCleanup). A fixture that commits
gives its temp repo an identity with ``init_repo`` / ``identity``: that writes only that
repo's own config, after checking git really resolves to that folder.

Used by every temp-repo fixture under orchestra/, scripts/, atrium/, tutor/ and keats/;
orchestra/test_git_hermetic.py checks each one leaves a GIT_DIR sentinel untouched.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

# Test identity, written only into a temp repo's own config. Literal test data.
NAME = "t"
EMAIL = "t@t"
# What the hermetic environment sets on top of dropping every GIT_* variable.
FIXED = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}


def is_git_var(name: str) -> bool:
    return name.upper().startswith("GIT_")


def hermetic_git(test) -> Path:
    """Make os.environ hermetic for git until ``test`` finishes; returns the empty global config.

    Call before the fixture's first git call. Restored by the test's cleanups, so a later
    test (or module, in a discover run) sees the environment it had before.
    """
    folder = Path(tempfile.mkdtemp(prefix="git-hermetic-"))
    test.addCleanup(shutil.rmtree, folder, True)
    config = folder / "gitconfig"
    config.write_text("", encoding="utf-8")
    patch = mock.patch.dict(os.environ)
    patch.start()
    test.addCleanup(patch.stop)
    for k in [k for k in os.environ if is_git_var(k)]:
        del os.environ[k]
    os.environ.update(FIXED, GIT_CONFIG_GLOBAL=str(config))
    return config


def _git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _same(a, b) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def leaked() -> list[str]:
    """The repo-locating variables (git's own list: rev-parse --local-env-vars) set right now."""
    names = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True,
                           cwd=tempfile.gettempdir(), env={k: v for k, v in os.environ.items()
                                                           if not is_git_var(k)}).stdout.split()
    return sorted(n for n in names if n in os.environ)


def _refuse_if_leaked(repo) -> None:
    bad = leaked()
    if bad:
        raise AssertionError(f"testing_git: {', '.join(bad)} set, so git in {repo} would act on another "
                             "repo; call testing_git.hermetic_git(self) first in setUp")


def identity(repo, name: str = NAME, email: str = EMAIL) -> None:
    """Give the temp repo at ``repo`` a commit identity in its own config, and nowhere else.

    Refuses (AssertionError) if a repo-locating GIT_* variable is set, or if git does not resolve
    ``repo`` to itself (a folder that is not a repo but sits inside another one)."""
    _refuse_if_leaked(repo)
    bare = _git(repo, "rev-parse", "--is-bare-repository") == "true"
    where = _git(repo, "rev-parse", "--absolute-git-dir") if bare else _git(repo, "rev-parse", "--show-toplevel")
    if not _same(where, repo):
        raise AssertionError(f"testing_git: {repo} resolves to the repo at {where}; not writing its config")
    _git(repo, "config", "user.name", name)
    _git(repo, "config", "user.email", email)


def init_repo(repo, *init_args: str, name: str = NAME, email: str = EMAIL) -> Path:
    """``git init`` a temp repo (``init_args`` e.g. "-b", "main") and give it a test identity."""
    repo = Path(repo)
    _refuse_if_leaked(repo)  # before `git init`: with GIT_DIR set it would re-initialise that repo
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", *init_args)
    identity(repo, name, email)
    return repo
