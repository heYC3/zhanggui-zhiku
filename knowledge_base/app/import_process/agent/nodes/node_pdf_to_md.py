import shutil
import time
import zipfile
from pathlib import Path

import requests

from app.conf.mineru_config import mineru_config
from app.import_process.agent.node_base import NodeBase
from app.import_process.agent.state import ImportGraphState, create_default_state
from app.core.logger import logger

class NodePdfToMd(NodeBase):
    """
    节点: PDF转Markdown (node_pdf_to_md)
    为什么叫这个名字: 核心任务是将 PDF 非结构化数据转换为 Markdown 结构化数据。
    未来要实现:
    1. 调用 MinerU (magic-pdf) 工具。
    2. 将 PDF 转换成 Markdown 格式。
    3. 将结果保存到 state["md_content"]。
    """

    # 覆盖基类的 name 属性，标识节点名称
    name: str = "node_pdf_to_md"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        """
        必要参数：task_id、pdf_path、local_dir
        更新参数：md_path、md_content
        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """

        # 步骤1：校验路径参数和输出目录
        pdf_path_obj, output_dir_obj = self._step_1_validate_paths(state)

        # 步骤2：上传PDF到MinerU并轮询解析结果
        zip_url = self._step_2_upload_and_poll(pdf_path_obj, output_dir_obj)

        # 步骤3：下载ZIP包并提取MD文件
        md_path = self._step_3_download_and_extract(zip_url, output_dir_obj, pdf_path_obj.stem)

        # 步骤4：读取md的内容
        with open(md_path, 'r', encoding="utf-8") as f:
            md_content = f.read()

        # 步骤5：更新state状态值
        state["md_content"] = md_content
        state["md_path"] = md_path

        return state

    def _step_3_download_and_extract(self, zip_url: str, output_dir_obj: Path, pdf_stem: str) -> str:

        # 1、下载ZIP包（失败自动重试，国内 CDN 偶发被代理掐断 SSL）
        logger.info(f"【ZIP下载】开始下载ZIP包：{zip_url} ...")
        response = None
        for attempt in range(1, 4):
            try:
                response = requests.get(zip_url, timeout=120)
                break
            except requests.exceptions.RequestException as e:
                logger.warning(f"【ZIP下载】第{attempt}次下载失败：{e}")
                if attempt == 3:
                    raise
                time.sleep(5 * attempt)

        if response is None or response.status_code != 200:
            raise RuntimeError(f"【ZIP下载】ZIP包下载失败：状态码：{response.status_code}")

        zip_save_path = output_dir_obj / f"{pdf_stem}_result.zip"
        with open(zip_save_path, "wb") as f:
            f.write(response.content)
        logger.info(f"【ZIP下载】ZIP包下载成功：保存路径：{zip_save_path}")

        # 2、清空旧的解压目录
        logger.info(f"【ZIP解压】开始解压ZIP包：{output_dir_obj} ...")
        extract_target_dir = output_dir_obj / pdf_stem

        # 清理旧目录
        if extract_target_dir.exists():
            try:
                shutil.rmtree(extract_target_dir)
                logger.info(f"【ZIP解压】已清空旧的解压目录：{extract_target_dir}")
            except Exception as e:
                logger.warning(f"【ZIP解压】清空旧的解压目录失败，但是不影响文件解压：{str(e)}")

        # 3、创建解压目录
        extract_target_dir.mkdir(parents=True, exist_ok=True)

        # 4、解压
        with zipfile.ZipFile(zip_save_path, "r") as zip_file_obj:
            zip_file_obj.extractall(extract_target_dir)
        logger.info(f"【ZIP解压】ZIP解压完成，解压目录：{extract_target_dir}")

        # 5、重命名
        logger.info(f"【MD重命名】找到MinerU生成的full.md文件")
        target_md_file = extract_target_dir / "full.md"
        logger.info(f"【MD重命名】开始将full.md文件进行重命名")
        new_md_path = target_md_file.with_name(f"{pdf_stem}.md")
        target_md_file.rename(new_md_path)
        logger.info(f"【MD重命名】重命名成功，文件名：{pdf_stem}.md")

        return str(new_md_path.absolute())


    def _step_2_upload_and_poll(self, pdf_path_obj: Path, output_dir_obj: Path):

        # 1、参数校验
        if not mineru_config.base_url or not mineru_config.api_token:
            raise ValueError("MinerU配置缺失：请在 .env 文件中正确配置 MINERU_API_TOKEN 和 MINERU_BASE_URL 参数")
        logger.info(f"【配置校验】MinerU配置校验成功，开始处理文件：{pdf_path_obj.name}")

        # 2、向MinerU服务器获取上传链接
        token = mineru_config.api_token
        url = f"{mineru_config.base_url}/file-urls/batch"
        header = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
        data = {
            "files": [
                {"name":pdf_path_obj.name}
            ],
            "model_version":"vlm"
        }
        logger.info(f"【获取上传链接】调用接口：{url}，请求参数：{data}")

        # 调用接口：获取上传url和任务的batch_id
        response = requests.post(url,headers=header,json=data, timeout=30)

        # 对响应结果进行校验：先校验http状态，再校验业务码
        if response.status_code != 200:
            raise RuntimeError(f"【获取上传链接】响应失败：状态码：{response.status_code}，响应结果：{response.text}")

        result = response.json()
        if result.get("code") != 0:
            raise RuntimeError(f"【获取上传链接】接口调用业务失败：返回数据：{result}")

        # 获取响应结果
        signed_url = result["data"]["file_urls"][0]
        batch_id = result["data"]["batch_id"]
        logger.info(f"【获取上传链接】成功：上传链接已生成，batch_id：{batch_id}")

        # 3、准备上传
        logger.info(f"【准备上传】开始上传PDF文件：{pdf_path_obj.name}")
        with open(pdf_path_obj, "rb") as f:
            file_data = f.read()

        try:
            # PDF上传
            put_resp = requests.put(url=signed_url, data=file_data, timeout=30)
            # 上传失败
            if put_resp.status_code != 200:
                raise RuntimeError(f"【文件上传】上传失败：状态码：{put_resp.status_code}")
            logger.info(f"【文件上传】上传成功，文件{pdf_path_obj.name}已进入云存储")
        except Exception as e:
            raise RuntimeError(f"【文件上传】上传失败：{str(e)}")


        # 4、轮询解析结果（batch_id）
        poll_url = f"{mineru_config.base_url}/extract-results/batch/{batch_id}"

        start_time = time.time() #记录开始时间
        timeout_seconds = 600 #最大超时时间
        poll_interval = 3 #轮询间隔时间
        logger.info(f"【任务轮询】开始轮询解析结果，请稍候...bactch_id：{batch_id}")

        # 根据batch_id轮询任务状态直到成功"done"
        while True:

            elapsed_time = time.time() - start_time
            if elapsed_time > timeout_seconds:
                raise TimeoutError(f"【任务轮询】超时，batch_id：{batch_id}")


            # 发起请求
            poll_resp = requests.get(poll_url, headers=header, timeout=10)

            # 校验HTTP状态
            if poll_resp.status_code != 200:
                raise RuntimeError(f"【任务轮询】请求失败，状态码：{poll_resp.status_code}，batch_id：{batch_id}")

            # 校验任务的业务状态
            poll_data = poll_resp.json()
            if poll_data.get("code") != 0:
                raise RuntimeError(f"【任务轮询】接口调用业务失败：返回数据：{poll_data}")

            extract_results = poll_data["data"]["extract_result"]

            # 结果为空，继续轮询（防御性编程）
            if not extract_results:
                logger.info(f"【任务轮询】结果为空：已耗时{int(elapsed_time)}s，继续等待")
                time.sleep(poll_interval)
                continue

            # 结果不为空，获取结果
            result_item = extract_results[0]
            state_status = result_item["state"]

            # 状态为 done
            if state_status == "done":
                logger.info(f"【任务轮询】解析任务完成！总耗时{int(elapsed_time)}s，bactch_id：{batch_id}")
                full_zip_url = result_item["full_zip_url"]

                if not full_zip_url:
                    raise RuntimeError(f"【任务轮询】任务轮询完成，但没有返回ZIP包下载链接，bactch_id：{batch_id}")

                logger.info(f"【任务轮询】返回ZIP包下载链接：{full_zip_url}")
                return full_zip_url

            elif state_status == "failed":
                # 带出 MinerU 返回的具体错误原因（可能是页数超限/文件损坏等）
                mineru_error = result_item.get("error_msg") or result_item.get("message") or "未提供详情"
                raise RuntimeError(f"【任务轮询】解析任务失败！batch_id：{batch_id}，MinerU原因：{mineru_error}")
            else:
                logger.info(f"【任务轮询】处理中... 已耗时{int(elapsed_time)}s，状态：{state_status}， batch_id：{batch_id}")
                time.sleep(poll_interval)


    def _step_1_validate_paths(self, state: ImportGraphState):
        """
        校验PDF文件路径和输出路径
        :param state:
        :return:
        """
        # 获取路径
        pdf_path = state.get("pdf_path", "").strip()
        local_dir = state.get("local_dir", "").strip()

        # 1、参数的非空校验
        if not pdf_path:
            raise ValueError("核心参数pdf_path缺失")
        if not local_dir:
            raise ValueError("核心参数local_dir缺失")

        # 2、路径转换
        pdf_path_obj = Path(pdf_path)
        output_dir_obj = Path(local_dir)

        # 3、检查PDF文件的有效性
        if not pdf_path_obj.exists():
            raise ValueError(f"PDF文件不存在，绝对路径: {pdf_path_obj.absolute()}")

        # 4、确保输出目录存在，不存在则自动创建
        if not output_dir_obj.exists():
            logger.info(f"输出目录不存在，开始创建：{output_dir_obj.absolute()}")
            # 可以递归创建，并且如果目录已存在也不报错
            output_dir_obj.mkdir(parents=True, exist_ok=True)

        return pdf_path_obj, output_dir_obj

if __name__ == "__main__":

    import os
    # 获取项目所在路径
    from app.utils.path_util import PROJECT_ROOT


    # 组装文件路径
    local_file= os.path.join("doc", "hak180产品安全手册.pdf")
    # 组装文件的绝对路径
    pdf_path = os.path.join(PROJECT_ROOT, local_file)
    # 组装输出路径
    local_dir = os.path.join(PROJECT_ROOT, "output")

    # 当前节点图状态初始值
    init_state = create_default_state(
        task_id="task_001",
        pdf_path=pdf_path,
        local_dir=local_dir
    )

    # 执行节点的业务调用
    node_pdf_to_md = NodePdfToMd()
    final_state = node_pdf_to_md(init_state)