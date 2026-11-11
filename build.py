"""Build gate: the test suite must be all green before packaging.

Usage:
    python build.py [exe-name]

Runs test_v2_units.py with the current interpreter, then PyInstaller
with longctx-proxy-gui.spec, then copies the exe into app/ (next to proxy/
and engine/ in the repo layout).
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    name = argv[1] if len(argv) > 1 else "longctx-proxy-gui-5.1.0"
    r = subprocess.run([sys.executable, str(ROOT / "test_v2_units.py")],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    tail = [l for l in out.splitlines() if l.strip().startswith("结果")]
    print(tail[-1] if tail else out[-800:])
    if r.returncode != 0 or not tail or "0 failed" not in tail[-1]:
        print("GATE: suite not all green, refusing to package")
        return 1
    r2 = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "longctx-proxy-gui.spec",
         "--noconfirm", "--distpath", str(ROOT / "dist"),
         "--workpath", str(ROOT / "build")], cwd=ROOT)
    if r2.returncode != 0:
        return r2.returncode
    src = ROOT / "dist" / f"{name}.exe"
    if src.exists():
        dst_dir = ROOT / "app"
        dst_dir.mkdir(exist_ok=True)
        dst = dst_dir / src.name
        shutil.copy2(src, dst)
        print(f"GATE: copied to {dst}")
        print("NOTE: put python_path.txt next to the exe (interpreter with "
              "httpx/tiktoken), or set LONGCTX_PY.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
