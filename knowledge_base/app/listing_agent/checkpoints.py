"""把 LangGraph 的内存检查点存进文件。

进程停了再打开，还停在「等人确认」那一步，可以继续确认或取消。
没有单独安装 Postgres 检查点包，所以用官方内存检查点，重启时再从文件读回。
内存检查点内部用了无法序列化的临时函数，落盘前先改成普通字典。
"""

from __future__ import annotations

import pickle
import threading
from collections import defaultdict
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver


def _namespace() -> dict:
    return defaultdict(dict)


def _to_plain(value):
    if isinstance(value, dict):
        return {key: _to_plain(item) for key, item in value.items()}
    return value


class CheckpointStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.saver = InMemorySaver()
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("rb") as handle:
            data = pickle.load(handle)
        storage = defaultdict(_namespace)
        for thread_id, namespaces in data["storage"].items():
            for name, checkpoints in namespaces.items():
                storage[thread_id][name].update(checkpoints)
        writes = defaultdict(dict)
        for key, bucket in data["writes"].items():
            writes[key].update(bucket)
        self.saver.storage = storage
        self.saver.writes = writes
        self.saver.blobs = dict(data["blobs"])

    def save(self) -> None:
        payload = {
            "storage": _to_plain(self.saver.storage),
            "writes": _to_plain(self.saver.writes),
            "blobs": _to_plain(self.saver.blobs),
        }
        temporary = self.path.with_suffix(".pkl.tmp")
        with self._lock:
            with temporary.open("wb") as handle:
                pickle.dump(payload, handle)
            temporary.replace(self.path)
