# 复现图片摘要调用，定位 KeyError('error') 的真实来源
import base64
import os
import traceback

from dotenv import load_dotenv

load_dotenv()

from langchain_core.messages import HumanMessage

from app.lm.lm_utils import get_llm_client
from app.conf.lm_config import lm_config

# 用一张真实手册图片测试
img = r"D:\研究生个人空间\Codex工作区\项目\掌柜智库\knowledge_base\output\20261005\a3c5f46f-26b9-4611-85fa-51885208f7cf\华为擎云L420 用户手册-(KelvinV,01,zh-cn)\images\0d067cf236e07a12c223adfaf1c18e759927771e63e0adb6c3401ae0057690b2.jpg"

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