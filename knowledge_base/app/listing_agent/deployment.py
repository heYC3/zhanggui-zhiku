"""公开测试版启动门：指定店铺、公开样例、单实例持久盘。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from app.listing_agent.paths import runtime_dir

TEST_STORE = "zhanggui-test.myshopify.com"


def public_mode() -> bool:
    return os.getenv("LISTING_DEPLOYMENT", "local").strip().lower() == "public"


def validate_public_config() -> None:
    if not public_mode():
        return
    required = {
        "LISTING_SHOP_BACKEND": "shopify",
        "SHOPIFY_STORE_DOMAIN": TEST_STORE,
        "LISTING_DRAFT_MODE": "offline",
        "LISTING_CHECKPOINT": "sqlite",
        "PYTHON_DOTENV_DISABLED": "1",
    }
    for key, expected in required.items():
        if os.getenv(key, "").strip() != expected:
            raise RuntimeError(f"公开测试版要求 {key}={expected}")
    if any(os.getenv(key, "").strip().lower() not in {"", "0", "false", "no"}
           for key in ("LISTING_TEST_MODE", "LISTING_USE_BGE")):
        raise RuntimeError("公开测试版不允许故障注入或本机 BGE")
    if not os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip():
        raise RuntimeError("公开测试版缺少 SHOPIFY_ADMIN_TOKEN")
    accounts = os.getenv("LISTING_ACCOUNTS", "").strip().split(",")
    parts = [piece.strip() for piece in accounts[0].split(":")]
    if (len(accounts) != 1 or len(parts) != 4 or not parts[0] or not parts[3]
            or parts[2] != "editor" or len(parts[1]) < 20
            or parts[1].lower() in {"demo-pass", "password"}
            or parts[1].lower().startswith(("replace-", "your-"))):
        raise RuntimeError("首版只允许一个编辑账号，密码至少 20 位，机构不能为空")
    runtime_text = os.getenv("LISTING_RUNTIME_DIR", "")
    if not runtime_text or not Path(runtime_text).is_absolute():
        raise RuntimeError("公开测试版必须显式指定绝对运行目录")
    platform = os.getenv("LISTING_PLATFORM", "container")
    if platform == "pythonanywhere":
        home = Path.home().resolve()
        runtime = runtime_dir().resolve()
        username = os.getenv("USER", "")
        if (not username or home != (Path("/home") / username).resolve()
                or home not in runtime.parents or runtime == home):
            raise RuntimeError("PythonAnywhere 运行目录必须位于当前账号的持久家目录中")
        if os.getenv("LISTING_PUBLIC_ORIGIN") != f"https://{username}.pythonanywhere.com":
            raise RuntimeError("PythonAnywhere 公开来源必须与当前账号子域一致")
        if os.getenv("LISTING_SHOPIFY_TRANSPORT") != "pythonanywhere":
            raise RuntimeError("PythonAnywhere 公开版要求官方代理出网")
        from app.listing_agent.shopify_adapter import _shopify_proxies
        _shopify_proxies()
        return
    if platform != "container":
        raise RuntimeError("不支持的部署平台")
    volume_text = os.getenv("LISTING_VOLUME_DIR", "")
    volume, runtime = Path(volume_text).resolve(), runtime_dir().resolve()
    if (not volume_text or not runtime_text or not Path(runtime_text).is_absolute()
            or volume not in runtime.parents or not os.path.ismount(volume)):
        raise RuntimeError("公开测试版运行目录必须位于 LISTING_VOLUME_DIR 持久盘内")


@contextmanager
def runtime_lease(runtime: Path):
    """服务与备份/恢复共用的非阻塞进程锁；锁放在运行目录外。"""
    runtime = Path(runtime).resolve()
    runtime.parent.mkdir(parents=True, exist_ok=True)
    path = runtime.parent / f".{runtime.name}.service.lock"
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("运行目录正被使用；请先停止服务或其他维护进程") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
