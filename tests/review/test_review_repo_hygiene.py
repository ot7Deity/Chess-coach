"""Repo-wide tools run from the main checkout must not walk into agent worktrees.

Agent worktrees live at .claude/worktrees/<name>/, *inside* the main checkout,
and each is a full copy of the repo: its own tests/, its own conftest.py, its
own .venv. A repo-wide tool run from the main checkout sees all of them:

* `ruff format .` reformatted other agents' uncommitted work (it happened).
* `pytest .` (or any explicit path above tests/) would collect every
  worktree's suite, and each worktree's conftest drops the schema of whatever
  DATABASE_URL the *caller* has set - so one command could run five agents'
  destructive fixtures against one database.
* `git add -A` from the main checkout would try to add the worktrees.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _mentions_claude(entries: list[str]) -> bool:
    return any(e.strip("/").startswith(".claude") for e in entries)


@pytest.mark.xfail(strict=True, reason="DISAGREEMENT: .claude/ is not excluded from ruff")
def test_ruff_skips_agent_worktrees():
    ruff = _pyproject().get("tool", {}).get("ruff", {})
    assert _mentions_claude(ruff.get("exclude", []) + ruff.get("extend-exclude", []))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DISAGREEMENT: pytest's norecursedirs does not include .claude, so `pytest .` "
        "from the main checkout collects every agent's suite and runs each one's "
        "schema-dropping fixtures against the caller's DATABASE_URL"
    ),
)
def test_pytest_does_not_recurse_into_agent_worktrees():
    ini = _pyproject().get("tool", {}).get("pytest", {}).get("ini_options", {})
    assert _mentions_claude(ini.get("norecursedirs", []))


@pytest.mark.xfail(strict=True, reason="DISAGREEMENT: .claude/ is not in .gitignore")
def test_git_ignores_agent_worktrees():
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert _mentions_claude([line for line in lines if not line.startswith("#")])
