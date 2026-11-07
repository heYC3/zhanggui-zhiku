"""内容寻址的本地手册图片；只向页面提供注册的栅格图片。"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

SUFFIXES = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
router = APIRouter()


def asset_root() -> Path:
    default = Path(__file__).resolve().parents[2] / 'output' / 'manual-assets'
    return Path(os.getenv('MANUAL_ASSET_DIR') or default).resolve()


def publish_image(path: str | Path) -> str:
    from PIL import Image

    source = Path(path)
    suffix = source.suffix.lower()
    if suffix not in SUFFIXES:
        raise ValueError('不支持的手册图片格式')
    with Image.open(source) as img:
        img.verify()
    data = source.read_bytes()
    name = hashlib.sha256(data).hexdigest() + suffix
    root = asset_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    if not target.exists():
        import tempfile
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as f:
            temporary = Path(f.name)
            f.write(data)
        try:
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return '/manual-assets/' + name


@router.get('/manual-assets/{name}')
def manual_image(name: str):
    if not re.fullmatch(r'[0-9a-f]{64}\.(?:jpg|jpeg|png|gif|webp|bmp)', name):
        raise HTTPException(404, '图片不存在')
    root = asset_root()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        raise HTTPException(404, '图片不存在')
    return FileResponse(path, headers={'Cache-Control': 'public, max-age=31536000, immutable',
                                       'X-Content-Type-Options': 'nosniff'})
