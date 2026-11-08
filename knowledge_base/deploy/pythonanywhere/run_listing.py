"""PythonAnywhere 的 Listing 启动器；私有配置与运行数据均放在家目录。"""

import argparse
import os
from pathlib import Path
import sys

from dotenv import dotenv_values
import uvicorn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--uds", required=True)
    args = parser.parse_args()
    home = Path.home().resolve()
    config = home / ".config/zhanggui/listing.env"
    if config.stat().st_mode & 0o077:
        raise RuntimeError("Listing 私有配置权限必须为 600")
    values = dotenv_values(config)
    for key in ("LISTING_ACCOUNTS", "SHOPIFY_STORE_DOMAIN", "SHOPIFY_ADMIN_TOKEN"):
        os.environ[key] = values.get(key) or ""
    os.environ.update({
        "LISTING_DEPLOYMENT": "public", "LISTING_PLATFORM": "pythonanywhere",
        "LISTING_RUNTIME_DIR": str(home / ".local/share/zhanggui/listing-runtime"),
        "LISTING_CHECKPOINT": "sqlite", "LISTING_SHOP_BACKEND": "shopify",
        "LISTING_DRAFT_MODE": "offline", "LISTING_USE_BGE": "0", "LISTING_TEST_MODE": "0",
        # ASGI worker 不继承 Bash 控制台的代理变量，显式指定平台官方出网。
        "https_proxy": "http://proxy.server:3128",
        "LISTING_SHOPIFY_TRANSPORT": "pythonanywhere", "PYTHON_DOTENV_DISABLED": "1",
        "LISTING_PUBLIC_ORIGIN": f"https://{home.name}.pythonanywhere.com",
    })
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    uvicorn.run("app.listing_agent.api:app", uds=args.uds, workers=1,
                proxy_headers=True, forwarded_allow_ips="*", log_level="info")


if __name__ == "__main__":
    main()
