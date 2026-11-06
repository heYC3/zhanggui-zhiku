"""
同时启动掌柜智库的三个网页服务。

- 8000：手册导入（上传 PDF / Markdown，走导入流程图）
- 8001：智能问答（走检索流程图，支持打字机式流式回答）
- 8002：Listing 工作台（检索证据、看草稿、确认后写入）
"""
import multiprocessing

import uvicorn


def _run(app_path: str, port: int) -> None:
    uvicorn.run(app_path, host="127.0.0.1", port=port, log_level="info")


def main() -> None:
    jobs = [
        ("app.import_process.api.file_import_service:app", 8000, "手册导入"),
        ("app.query_process.api.query_service:app", 8001, "智能问答"),
        ("app.listing_agent.api:app", 8002, "Listing 工作台"),
    ]
    processes = []
    for app_path, port, title in jobs:
        process = multiprocessing.Process(
            target=_run,
            args=(app_path, port),
            name=title,
        )
        process.start()
        processes.append(process)
        print(f"{title} 已启动：http://127.0.0.1:{port}")

    print("导入页面：http://127.0.0.1:8000/import.html")
    print("问答页面：http://127.0.0.1:8001/chat.html")
    print("Listing 页面：http://127.0.0.1:8002/listing.html")
    print("按 Ctrl+C 停止。")

    try:
        for process in processes:
            process.join()
    except KeyboardInterrupt:
        for process in processes:
            process.terminate()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
