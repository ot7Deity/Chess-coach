"""Repo-wide tools run from the main checkout must not walk into agent worktrees.

Agent worktrees live at .claude/worktrees/<name>/, *inside* the main checkout,
and each is a full copy of the repo with its own tests/ and conftest.py. These
tests check what the tools actually *do*, not what the config files say: each
builds a miniature checkout in tmp_path with this repo's real pyproject.toml, a
trap file under .claude/, and runs the real tool on it.

A correction, kept here on purpose: an earlier version of this file asserted
that `.claude` appeared in pytest's `norecursedirs` setting, failed, and was
reported as a finding. It was wrong. pytest's *default* norecursedirs already
contains '.*', which matches `.claude`, and setting nothing keeps the default;
the ini value was the wrong thing to assert on. Each test below has a control
showing the same tool does reach the trap when it is not under a dot directory,
so a pass means the exclusion works rather than that the tool saw nothing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _mini_checkout(root: Path, trap_dir: str) -> None:
    shutil.copy(REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
    (root / "tests").mkdir()
    (root / "tests" / "test_main.py").write_text("def test_main():\n    pass\n")
    trap = root / trap_dir / "tests"
    trap.mkdir(parents=True)
    # A failing conftest: if pytest so much as imports it, collection errors.
    (trap / "conftest.py").write_text("raise RuntimeError('walked into a worktree')\n")
    (trap / "test_trap.py").write_text("import os\n\n\ndef test_trap():\n    pass\n")


def _collect(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "."],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_pytest_dot_does_not_collect_agent_worktrees(tmp_path: Path):
    _mini_checkout(tmp_path, ".claude/worktrees/other-agent")
    proc = _collect(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "test_main" in proc.stdout
    assert "test_trap" not in proc.stdout
    assert "walked into a worktree" not in proc.stdout + proc.stderr


def test_control_pytest_does_collect_the_same_tree_outside_a_dot_directory(tmp_path: Path):
    _mini_checkout(tmp_path, "worktrees/other-agent")
    proc = _collect(tmp_path)
    assert "walked into a worktree" in proc.stdout + proc.stderr


def _ruff(root: Path) -> subprocess.CompletedProcess[str]:
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff not on PATH")
    return subprocess.run(
        [ruff, "check", "--no-cache", "--output-format", "concise", "."],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_ruff_dot_does_not_lint_agent_worktrees(tmp_path: Path):
    _mini_checkout(tmp_path, ".claude/worktrees/other-agent")
    proc = _ruff(tmp_path)
    assert "test_trap.py" not in proc.stdout, proc.stdout
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_control_ruff_does_lint_the_same_file_outside_a_dot_directory(tmp_path: Path):
    _mini_checkout(tmp_path, "worktrees/other-agent")
    proc = _ruff(tmp_path)
    assert "test_trap.py" in proc.stdout  # F401: `os` imported but unused


def test_git_ignores_agent_worktrees():
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    entries = [line.strip("/") for line in lines if line and not line.startswith("#")]
    assert any(e.startswith(".claude") for e in entries)
