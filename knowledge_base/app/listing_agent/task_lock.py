"""同一任务的编辑、确认与核对串行执行；文件锁随进程退出自动释放。"""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import threading

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


@contextmanager
def task_lock(runtime: Path, task_id: str):
    directory = Path(runtime).resolve() / "task_locks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(task_id.encode()).hexdigest() + ".lock")
    with _guard:
        lock = _locks.setdefault(str(path), threading.RLock())
    with lock, path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            # LK_LOCK 只重试约 10 秒；等待网络操作时使用显式阻塞重试。
            import time
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def serialized(method):
    def wrapped(self, task_id, *args, **kwargs):
        with task_lock(self.runtime, task_id):
            return method(self, task_id, *args, **kwargs)
    from functools import wraps
    return wraps(method)(wrapped)
