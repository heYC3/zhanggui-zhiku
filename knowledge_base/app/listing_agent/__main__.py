"""命令行启动 Listing 网页：python -m app.listing_agent"""

import os

import uvicorn


def main() -> None:
    from app.listing_agent.shopify_adapter import load_env_file
    load_env_file()
    uvicorn.run(
        "app.listing_agent.api:app",
        host=os.getenv("LISTING_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT") or os.getenv("LISTING_PORT", "8002")),
        workers=1,
        log_level="info",
    )


if __name__ == "__main__":
    main()
