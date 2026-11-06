"""命令行启动 Listing 网页：python -m app.listing_agent"""

import uvicorn


def main() -> None:
    uvicorn.run("app.listing_agent.api:app", host="127.0.0.1", port=8002, log_level="info")


if __name__ == "__main__":
    main()
