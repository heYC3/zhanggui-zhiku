"""Listing 模块用到的目录。

导入和问答仍在 knowledge_base/app 里。
本模块只读写 listing_data，不改手册库。
"""

from __future__ import annotations

import os
from pathlib import Path


# paths.py → listing_agent → app → knowledge_base
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PACKAGE_ROOT / "listing_data"
PRODUCT_DIR = DATA_DIR / "products"
PLATFORM_DIR = DATA_DIR / "platforms"
EVAL_DIR = DATA_DIR / "eval"
DEV_PATH = EVAL_DIR / "dev.json"
FROZEN_PATH = EVAL_DIR / "frozen_test.json"
EVAL_PATH = FROZEN_PATH
MANIFEST_PATH = EVAL_DIR / "tasks.json"
MODEL_CARD_PATH = DATA_DIR / "model_card.json"
REPORT_PATH = DATA_DIR / "reports" / "strategy_report.json"
BASELINE_PATH = DATA_DIR / "reports" / "baseline.md"


def runtime_dir() -> Path:
    """检查点、台账和 trace 的落盘目录。测试可以换到临时文件夹。"""
    return Path(os.getenv("LISTING_RUNTIME_DIR", str(DATA_DIR / "runtime")))
