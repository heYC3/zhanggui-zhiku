# 复现图片摘要调用，定位 KeyError('error') 的真实来源
import base64
import os
import traceback

from dotenv import load_dotenv

load_dotenv()

from langchain_core.messages import HumanMessage

from app.lm.lm_utils import get_llm_client
from app.conf.lm_config import lm_config

# 填本机图片路径后再运行，例如 REPRO_VLM_IMAGE=/path/to/page.jpg
img = os.environ.get("REPRO_VLM_IMAGE", "")
if not img:
    raise SystemExit("请设置环境变量 REPRO_VLM_IMAGE 为本机图片路径")

with open(img, "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

messages = [
    HumanMessage(content=[
        {"type": "text", "text": "用50字以内中文描述这张图片"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ])
]

try:
    client = get_llm_client(model=lm_config.lv_model)
    resp = client.invoke(messages)
    print("成功:", resp.content[:80])
except Exception:
    print("异常类型:", type(Exception).__name__)
    traceback.print_exc()
    print("---- 最内层异常 ----")
    import sys
    tb = sys.exc_info()
    print("类型:", tb[0].__name__, "消息:", tb[1])