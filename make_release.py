"""把 dist/cxmon 打包成可以直接上传 GitHub Releases 的 zip。

为什么这段活儿用 Python 而不是写在 .bat 里：
给用户看的说明文件叫「使用说明.txt」，路径里有中文。而 .bat 必须是纯 ASCII
（cmd.exe 用系统 ANSI 码页解析 .bat，UTF-8 中文会把命令撕碎 —— 实测
"errorlevel" 会变成 "ofile"，导致双击毫无反应）。所以带中文路径的复制和压缩
交给 Python，Python 处理 UTF-8 路径没有问题。

跑之前先执行 PyInstaller（见 打包exe.bat）。
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "dist" / "cxmon"
USAGE_SRC = ROOT / "使用说明.txt"
USAGE_DST = SRC / "使用说明.txt"
HASH_DST = SRC / "SHA256.txt"
ZIP = ROOT / "dist" / "cxmon-windows-x64.zip"

# 这些是本地运行产物，绝不能进发布包（config.json 里装着 Cookie 和推送 key）
JUNK = ("config.json", "state.json", "state.json.tmp", "monitor.log",
        "monitor.pid", "tray.pid", "panel.pid", "panel.show")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def main() -> int:
    # 输出一律用 ASCII：构建脚本在 cmd 控制台里跑，中文会因为码页
    # （chcp / OEM 936 / UTF-8 之间来回换算）变成乱码。这个项目已经因为
    # 码页问题踩过一次坑，构建链路上不再赌编码。
    if not SRC.is_dir():
        print(f"[ERROR] not found: {SRC}")
        print("        Run PyInstaller first (double-click the build .bat).")
        return 1

    if USAGE_SRC.exists():
        shutil.copy2(USAGE_SRC, USAGE_DST)
        print("[1/3] usage doc copied into the bundle")
    else:
        print(f"[WARN] missing {USAGE_SRC.name} - bundle will have no usage doc")

    print("[2/3] removing local runtime files (never ship credentials)")
    cleaned = 0
    for name in JUNK:
        p = SRC / name
        if p.exists():
            p.unlink()
            print(f"        - removed {name}")
            cleaned += 1
    if not cleaned:
        print("        - nothing to remove")

    print("[3/3] computing hash and zipping ...")

    # exe 的校验值随包一起发给用户，比写在发布页上更可靠
    # （手工把哈希抄进发布说明一定会抄错 —— 第一次发布就抄错了）
    exe = SRC / "cxmon.exe"
    digest = _sha256(exe) if exe.exists() else ""
    if digest:
        HASH_DST.write_text(
            f"# cxmon.exe 的 SHA256 校验值\n"
            f"# 校验方法（PowerShell）: Get-FileHash .\\cxmon.exe -Algorithm SHA256\n"
            f"{digest}  cxmon.exe\n",
            encoding="utf-8")
        print(f"        cxmon.exe SHA256 = {digest}")
    else:
        print("        [WARN] 没找到 cxmon.exe，跳过校验值")

    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(SRC.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(SRC))

    # 自检：发布包里绝不能出现凭据/日志类文件
    with zipfile.ZipFile(ZIP) as z:
        names = z.namelist()
        leaked = [n for n in names
                  if Path(n).name in JUNK or n.endswith(".pid")]
        top = sorted({n.split("/")[0] for n in names})

    if leaked:
        print(f"[FAIL] privacy check: these must not be in the zip: {leaked}")
        return 1

    size_mb = ZIP.stat().st_size / 1024 / 1024
    print("[ OK ] privacy check passed (no config.json / logs / pid)")
    print()
    print(f"  release : {ZIP}  ({size_mb:.1f} MB)")
    print(f"  top     : {', '.join(top[:6])}")
    if digest:
        print(f"  sha256  : {digest}")
        print("            (also written to SHA256.txt inside the zip)")
    print()
    print("  next    : upload this zip to GitHub Releases (NOT into the repo).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
