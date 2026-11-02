"""Build a self-contained Windows candidate using an explicitly supplied CPython.

No runtime is downloaded and no package is installed into the supplied runtime.
Dependency wheels must match requirements.lock hashes. The output must be new.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib
import zipfile


REPOSITORY = Path(__file__).resolve().parents[1]
IGNORED_NAMES = {"site-packages", "__pycache__", "test", "tests", ".git", ".pytest_cache"}
RUNTIME_ROOT_IGNORES = {"include", "libs", "Scripts", "tcl"}
STDLIB_IGNORES = {"idlelib", "tkinter", "turtledemo", "ensurepip"}


def sha256(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def is_link(path):
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def safe_path(path):
    path = Path(path).expanduser().absolute()
    for current in (path, *path.parents):
        if current.exists() and is_link(current):
            raise ValueError(f"linked_path_not_allowed: {current}")
    return path.resolve()


def run(arguments, *, cwd=None):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run([str(a) for a in arguments], cwd=cwd, env=environment, check=True)


def copy_runtime(source, target):
    def ignore(directory, names):
        directory = Path(directory)
        excluded = set()
        for name in names:
            lower = name.lower()
            if name in IGNORED_NAMES or lower.endswith((".pyc", ".pyo", ".pdb")):
                excluded.add(name)
            elif directory == source and name in RUNTIME_ROOT_IGNORES:
                excluded.add(name)
            elif directory == source / "Lib" and name in STDLIB_IGNORES:
                excluded.add(name)
            elif directory == source / "DLLs" and (lower.startswith(("_test", "tcl", "tk", "_tkinter"))):
                excluded.add(name)
            elif is_link(directory / name):
                raise ValueError(f"linked_runtime_entry: {directory / name}")
        return excluded

    shutil.copytree(source, target, ignore=ignore)
    if not (target / "LICENSE.txt").is_file():
        raise ValueError("cpython_license_missing")
    # Isolate registry, user site packages, PYTHONPATH, and an unrelated cwd.
    # Multiprocessing children use this same interpreter and search path.
    (target / "python312._pth").write_text(".\nLib\nDLLs\nLib/site-packages\nimport site\n", encoding="utf-8")


def project_wheel(repository, target):
    config = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    name = re.sub(r"[-_.]+", "_", config["name"])
    version = config["version"]
    wheel_path = target / f"{name}-{version}-py3-none-any.whl"
    dist = f"{name}-{version}.dist-info"
    source = repository / "src"
    entries = {}
    for path in sorted((source / "yuecang").rglob("*")):
        relative = path.relative_to(source).as_posix()
        if "__pycache__" in path.parts or not path.is_file():
            continue
        if is_link(path):
            raise ValueError(f"linked_source_entry: {relative}")
        if path.suffix == ".py" or relative.startswith("yuecang/web/dist/"):
            entries[relative] = path.read_bytes()
    if "yuecang/web/dist/index.html" not in entries or not any(k.startswith("yuecang/web/dist/assets/") for k in entries):
        raise ValueError("built_frontend_missing: build web/dist before packaging")
    metadata = ["Metadata-Version: 2.3", f"Name: {config['name']}", f"Version: {version}",
                f"Summary: {config.get('description', '')}", f"Requires-Python: {config['requires-python']}",
                *(f"Requires-Dist: {value}" for value in config.get("dependencies", [])), "", ""]
    entries[dist + "/METADATA"] = "\n".join(metadata).encode("utf-8")
    entries[dist + "/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: yuecang-portable-builder\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    entries[dist + "/entry_points.txt"] = b"[console_scripts]\nyuecang = yuecang.cli:main\n"
    for filename in ("LICENSE", "LICENSE.txt", "NOTICE"):
        if (repository / filename).is_file():
            entries[dist + "/licenses/" + filename] = (repository / filename).read_bytes()
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for filename, content in sorted(entries.items()):
        checksum = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        writer.writerow((filename, "sha256=" + checksum, len(content)))
    writer.writerow((dist + "/RECORD", "", ""))
    entries[dist + "/RECORD"] = record.getvalue().encode("utf-8")
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, content in sorted(entries.items()):
            info = zipfile.ZipInfo(filename, (2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return wheel_path, {k: hashlib.sha256(v).hexdigest() for k, v in entries.items() if not k.startswith(dist + "/")}


def license_inventory(output, site):
    packages = []
    for distribution in sorted(importlib.metadata.distributions(path=[str(site)]), key=lambda d: (d.metadata["Name"] or "").lower()):
        licenses = []
        for entry in distribution.files or []:
            basename = Path(str(entry)).name.lower()
            if basename.startswith(("license", "copying", "notice", "authors")) or "licenses" in Path(str(entry)).parts:
                path = Path(distribution.locate_file(entry)).resolve()
                if path.is_file() and path.is_relative_to(output):
                    licenses.append(path.relative_to(output).as_posix())
        packages.append({"name": distribution.metadata["Name"], "version": distribution.version,
                         "license_expression": distribution.metadata.get("License-Expression", ""),
                         "license_files": sorted(set(licenses))})
    notice = ["YueCang Windows 便携运行候选", "", "CPython 许可证：runtime/LICENSE.txt", "",
              "第三方依赖许可证随各软件包原样保留。完整文件位置见 licenses.json。",
              "OCR 可选组件 Tesseract 未打包；需要时安装并提供 eng/chi_sim 语言包。未安装会显示明确问题。", "",
              *(f"{p['name']}=={p['version']}" for p in packages), ""]
    (output / "THIRD-PARTY-NOTICES.txt").write_text("\n".join(notice), encoding="utf-8")
    (output / "licenses.json").write_text(json.dumps(packages, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return packages


def build(options):
    if os.name != "nt":
        raise ValueError("windows_build_host_required")
    runtime = safe_path(options.python_runtime)
    output = safe_path(options.output)
    repository = safe_path(options.repository)
    uv = safe_path(options.uv) if options.uv else Path(shutil.which("uv") or "")
    if not uv.is_file():
        raise ValueError("uv_not_found: supply --uv explicitly")
    if not (runtime / "python.exe").is_file() or not (runtime / "python312.dll").is_file():
        raise ValueError("explicit_cpython_312_directory_required")
    runtime_info = json.loads(subprocess.check_output([str(runtime / "python.exe"), "-I", "-B", "-c",
        "import sys,struct,json; print(json.dumps({'version':sys.version.split()[0], 'bits':struct.calcsize('P')*8, 'platform':sys.platform}))"], text=True))
    if not runtime_info["version"].startswith("3.12.") or runtime_info["bits"] != 64 or runtime_info["platform"] != "win32":
        raise ValueError("cpython_312_windows_x64_required")
    if output.exists() or output.is_relative_to(runtime) or output.is_relative_to(repository):
        raise ValueError("output_must_be_new_and_outside_runtime_and_repository")
    lock = repository / "requirements.lock"
    if not lock.is_file():
        raise ValueError("requirements_lock_missing")
    output.mkdir(parents=True)
    (output / "BUILD-INCOMPLETE.txt").write_text("Packaging in progress. Do not use this directory yet.\n", encoding="utf-8")
    started = time.time()
    portable_runtime = output / "runtime"
    copy_runtime(runtime, portable_runtime)
    site = portable_runtime / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    common = [uv, "pip", "install", "--python", portable_runtime / "python.exe", "--target", site, "--link-mode", "copy", "--no-deps"]
    if options.cache_dir:
        common.extend(["--cache-dir", safe_path(options.cache_dir)])
    if options.offline:
        common.append("--offline")
    run([*common, "--require-hashes", "--only-binary", ":all:", "--requirements", lock])
    wheel, source_hashes = project_wheel(repository, output)
    run([*common, wheel])
    wheel.unlink()
    shutil.copyfile(lock, output / "requirements.lock")
    launcher = '@echo off\r\nsetlocal\r\nset "PYTHONHOME="\r\nset "PYTHONPATH="\r\nset "PYTHONNOUSERSITE=1"\r\nset "PYTHONDONTWRITEBYTECODE=1"\r\n"%~dp0runtime\\python.exe" -I -B -m yuecang.cli %*\r\nexit /b %errorlevel%\r\n'
    (output / "start.cmd").write_bytes(launcher.encode("ascii"))
    (output / "README.txt").write_text("阅藏本地图书整理工作台\n\n双击 start.cmd 打开本机管理界面。\n指定书库：start.cmd --project \"D:\\Books\\MyLibrary\"\n验证书库：start.cmd --project \"D:\\Books\\MyLibrary\" verify\n书库默认位于当前用户的 LocalAppData/YueCang/Library，产品目录不存放图书。\n此目录自带 Python 与所需 Python 依赖；运行时无需共享基础设施、uv 或网络安装。\n可选 OCR 依赖 Tesseract 及语言包，尚未打包；无组件时显示问题并保留原件。\nAI 需在应用设置填写自己的服务地址/模型；不包含密钥。\n", encoding="utf-8")
    for documentation in (repository / "README.md", repository / "docs"):
        if not documentation.exists():
            raise ValueError("product_documentation_missing")
        for entry in ([documentation] if documentation.is_file() else [documentation, *documentation.rglob("*")]):
            if is_link(entry):
                raise ValueError("linked_documentation_entry")
        if documentation.is_dir():
            shutil.copytree(documentation, output / documentation.name)
        else:
            shutil.copyfile(documentation, output / documentation.name)
    packages = license_inventory(output, site)
    # Make sure native extension wheels and frontend all load from the candidate.
    run([portable_runtime / "python.exe", "-I", "-B", "-c", "import fastapi,httpx,uvicorn,lxml.etree,markdown_it,pypdf,PIL.Image,pypdfium2,yuecang; print('portable-imports: ok')"], cwd=output)
    files = [{"path": p.relative_to(output).as_posix(), "bytes": p.stat().st_size, "sha256": sha256(p)}
             for p in sorted(output.rglob("*")) if p.is_file() and p.name != "BUILD-INCOMPLETE.txt"]
    manifest = {"schema_version": 1, "kind": "yuecang-windows-portable", "complete": True,
                "python": runtime_info, "requirements_sha256": sha256(lock), "source_files": source_hashes,
                "packages": packages, "files": files, "file_count": len(files),
                "bytes": sum(f["bytes"] for f in files), "elapsed_seconds": round(time.time() - started, 2)}
    (output / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (output / "BUILD-INCOMPLETE.txt").unlink()
    actual = sum(p.stat().st_size for p in output.rglob("*") if p.is_file())
    print(json.dumps({"output": str(output), "bytes": actual, "mib": round(actual / 1024**2, 2),
                      "packages": len(packages), "manifest": str(output / "build-manifest.json")}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-runtime", type=Path, required=True, help="existing CPython 3.12 x64 directory, never downloaded")
    parser.add_argument("--output", type=Path, required=True, help="new candidate directory outside the source checkout")
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    parser.add_argument("--uv", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--offline", action="store_true")
    build(parser.parse_args())
