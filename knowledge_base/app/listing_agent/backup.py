"""停服备份完整运行目录；恢复只接受空目录，不覆盖现有数据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import zipfile

from app.listing_agent.deployment import runtime_lease
from app.listing_agent.paths import runtime_dir


def _check_databases(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in {".db", ".sqlite", ".sqlite3"}:
            # SQLite 的只读打开仍可能生成/更新 WAL 索引；验证副本避免改动原件。
            with tempfile.TemporaryDirectory(prefix="listing-db-check-") as directory:
                copy = Path(directory) / path.name
                shutil.copyfile(path, copy)
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(path) + suffix)
                    if sidecar.exists():
                        shutil.copyfile(sidecar, Path(str(copy) + suffix))
                connection = sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)
                try:
                    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                        raise ValueError(f"SQLite 完整性校验失败：{path.name}")
                finally:
                    connection.close()


def backup(runtime: Path, output: Path) -> dict:
    runtime, output = Path(runtime).resolve(), Path(output).resolve()
    if not runtime.is_dir() or output == runtime or runtime in output.parents:
        raise ValueError("运行目录必须存在，备份文件必须位于运行目录之外")
    output.parent.mkdir(parents=True, exist_ok=True)
    with runtime_lease(runtime):
        _check_databases(runtime)
        manifest = {"format": 1, "files": {}}
        # x 模式禁止覆盖以前的备份。
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(runtime.rglob("*")):
                if path.is_symlink():
                    raise ValueError("运行目录不允许符号链接")
                if not path.is_file():
                    continue
                name = path.relative_to(runtime).as_posix()
                if name == "backup_manifest.json":
                    raise ValueError("运行目录存在保留文件名 backup_manifest.json")
                data = path.read_bytes()
                manifest["files"][name] = hashlib.sha256(data).hexdigest()
                archive.writestr(name, data)
            archive.writestr("backup_manifest.json", json.dumps(manifest, ensure_ascii=False))
    return {"file_count": len(manifest["files"]), "output": str(output)}


def restore(source: Path, runtime: Path) -> dict:
    source, runtime = Path(source).resolve(), Path(runtime).resolve()
    with runtime_lease(runtime):
        if runtime.exists() and (not runtime.is_dir() or any(runtime.iterdir())):
            raise ValueError("恢复目标必须为空，禁止覆盖现有运行数据")
        stage = Path(tempfile.mkdtemp(prefix=".listing-restore-", dir=runtime.parent))
        try:
            with zipfile.ZipFile(source) as archive:
                names = archive.namelist()
                if len(names) != len(set(names)):
                    raise ValueError("备份包含重复文件名")
                manifest = json.loads(archive.read("backup_manifest.json"))
                files = manifest.get("files", {})
                if manifest.get("format") != 1 or not isinstance(files, dict):
                    raise ValueError("不支持的备份格式")
                if set(names) != set(files) | {"backup_manifest.json"}:
                    raise ValueError("备份文件清单不一致")
                for name, digest in files.items():
                    relative = PurePosixPath(name)
                    if (not name or relative.is_absolute() or ".." in relative.parts
                            or ":" in name or "\\" in name):
                        raise ValueError("备份包含不安全的路径")
                    data = archive.read(name)
                    if hashlib.sha256(data).hexdigest() != digest:
                        raise ValueError(f"备份校验失败：{name}")
                    target = stage.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            _check_databases(stage)
            if runtime.exists():
                runtime.rmdir()  # 上面已经确认目标为空。
            os.replace(stage, runtime)
            return {"file_count": len(files), "runtime": str(runtime)}
        finally:
            if stage.exists():
                if stage.resolve().parent != runtime.parent or not stage.name.startswith(".listing-restore-"):
                    raise RuntimeError("恢复临时目录校验失败")
                shutil.rmtree(stage)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("--runtime", type=Path, default=runtime_dir())
    args = parser.parse_args()
    result = backup(args.runtime, args.archive) if args.action == "backup" else restore(args.archive, args.runtime)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
