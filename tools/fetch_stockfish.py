"""Download the pinned Stockfish build into vendor/stockfish/.

Run with: uv run python tools/fetch_stockfish.py

Three things this does that a one-line curl would not:

1. **Verifies the download** against a SHA-256 pinned in `stockfish.lock.json`
   before the binary is ever executed.
2. **Picks the right SIMD build for this CPU** by trying the fastest variant
   first and smoke-testing it. The AVX-VNNI build is noticeably faster on recent
   Intel chips but raises an illegal-instruction fault on older CPUs - including
   the Ice Lake servers GitHub Actions uses - so a silent fallback chain matters.
3. **Pins the version.** Cached evaluations are only valid for the engine that
   produced them, so dev, CI and production must all run the same Stockfish.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = Path(__file__).resolve().parent / "stockfish.lock.json"
VENDOR_DIR = REPO_ROOT / "vendor" / "stockfish"
CHUNK = 1 << 20


def log(msg: str) -> None:
    print(f"[fetch-stockfish] {msg}", flush=True)


def platform_key() -> str:
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Linux":
        return "linux"
    raise SystemExit(f"Unsupported platform {system!r}. Add it to stockfish.lock.json.")


def download(url: str, dest: Path) -> str:
    """Download to `dest`, returning the hex SHA-256 of what arrived."""
    digest = hashlib.sha256()
    with urllib.request.urlopen(url) as response, dest.open("wb") as fh:
        total = int(response.headers.get("Content-Length") or 0)
        seen = 0
        while chunk := response.read(CHUNK):
            fh.write(chunk)
            digest.update(chunk)
            seen += len(chunk)
            if total:
                pct = 100 * seen // total
                print(f"\r[fetch-stockfish]   {pct:3d}%  {seen >> 20:4d} MiB", end="", flush=True)
    print()
    return digest.hexdigest()


def extract(archive: Path, into: Path) -> Path:
    """Extract `archive` into a fresh directory and return the engine binary.

    The archive comes from the internet, so it is unpacked into its own empty
    directory and members are filtered rather than trusted.
    """
    into.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(into)
    else:
        with tarfile.open(archive) as tf:
            # filter="data" refuses absolute paths, parent traversal, symlinks
            # and device files: a tar from the internet should not be able to
            # write outside the directory we chose for it.
            tf.extractall(into, filter="data")

    candidates = [
        p
        for p in into.rglob("*")
        if p.is_file()
        and p.name.startswith("stockfish")
        and p.suffix in ("", ".exe")
        and "nnue" not in p.name
    ]
    if not candidates:
        raise SystemExit(f"No stockfish binary found inside {archive.name}")
    # Prefer the shallowest match, which is the real binary rather than a doc or src copy.
    return min(candidates, key=lambda p: (len(p.parts), len(p.name)))


def smoke_test(binary: Path) -> bool:
    """Return True if the binary can actually *search* on this CPU.

    A wrong SIMD build does not fail gracefully - it dies with an illegal
    instruction - so the only reliable check is to run it.

    The probe must include a real search, not just the UCI handshake. The
    handshake does not touch the vectorised NNUE evaluation code, so an
    AVX-VNNI binary on a CPU without AVX-VNNI answers `uci` quite happily and
    only dies on `go`. An earlier version of this script checked the handshake
    alone, installed the wrong build, and the illegal instruction surfaced
    later as a confusing test failure. Hence `go depth 8` and a demand for
    `bestmove`.
    """
    try:
        proc = subprocess.run(
            [str(binary)],
            input="uci\nisready\nposition startpos\ngo depth 8\nquit\n",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"  smoke test could not run: {exc}")
        return False

    if proc.returncode != 0:
        log(f"  smoke test exited {proc.returncode} (wrong SIMD build for this CPU)")
        return False
    if "uciok" not in proc.stdout:
        log("  smoke test did not get 'uciok'")
        return False
    if "bestmove" not in proc.stdout:
        log("  engine answered the handshake but could not complete a search")
        return False

    first = next((ln for ln in proc.stdout.splitlines() if ln.startswith("id name")), "")
    log(f"  OK - {first.removeprefix('id name ').strip() or 'engine responded'} (searched)")
    return True


def main() -> int:
    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    tag = lock["tag"]
    repo = lock["repo"]
    key = platform_key()
    assets: list[str] = lock["preference"][key]

    final_name = "stockfish.exe" if key == "windows" else "stockfish"
    installed = VENDOR_DIR / final_name
    if installed.exists():
        log(f"{installed.relative_to(REPO_ROOT)} already present; verifying it runs")
        if smoke_test(installed):
            log("nothing to do")
            return 0
        log("existing binary does not run here; re-fetching")

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    (VENDOR_DIR / ".gitignore").write_text("*\n", encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="stockfish-") as tmp:
        tmpdir = Path(tmp)
        for asset in assets:
            meta = lock["assets"][asset]
            url = f"https://github.com/{repo}/releases/download/{tag}/{asset}"
            log(f"trying {asset} ({meta['size'] >> 20} MiB)")

            archive = tmpdir / asset
            actual = download(url, archive)
            if actual != meta["sha256"]:
                log(f"  CHECKSUM MISMATCH\n    expected {meta['sha256']}\n    got      {actual}")
                log("  refusing to execute an unverified binary")
                return 2
            log("  checksum OK")

            unpacked = tmpdir / f"unpacked-{asset}"
            binary = extract(archive, unpacked)
            binary.chmod(0o755)

            if not smoke_test(binary):
                continue

            shutil.copy2(binary, installed)
            installed.chmod(0o755)
            log(f"installed {installed.relative_to(REPO_ROOT)} ({tag}, from {asset})")
            log(f"set STOCKFISH_PATH={installed.relative_to(REPO_ROOT).as_posix()} in your .env")
            return 0

    log("every candidate build failed to run on this CPU")
    return 1


if __name__ == "__main__":
    sys.exit(main())
