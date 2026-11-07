"""店铺写入适配器接口（掌柜智库 Listing 用）

放的位置（按 2026-11-07 主对话里定好的路径）：
    knowledge_base/app/listing_agent/shop_adapter.py

当前做法：
- 新增 ShopConflictError：远端已有商品 / 已有副作用、但不合
  只建草稿的目标时用它，与“明确没写进去”的 ShopError 分开。
  收到它时禁止自动重建、禁止自动删除或发布，只能人工核对。
- ShopError 语义收紧：只表示“明确没写进去、无副作用，
  修好可重试”。状态不明一律是 ShopUnknownError。
- get_product() 三态收紧：只有响应里明确 product: null
  才返回 None；端点 404、401/403、响应缺字段一律抛
  ShopUnknownError（查询未知）。
- reconcile() 语义收紧：一次空搜索不能证明未创建；
  已发出但结果不明的操作，空搜索返回 unknown，不是
  not_found。区分“明确未发出”（可返回 not_found）与
  “已发出但结果未知”（只能 unknown），见 reconcile() 约定。
- 核对必须验正文与摘要：只凭标题 / 厂商 / 状态不能算
  found；操作标记、正文、摘要都要对上。
- WriteIntent 发送前校验：店铺、操作 ID、摘要、任务 /
  版本 / 幂等键缺一不可。台账唯一性与并发占用由业务层实现，
  适配器只做输入校验。

当前进度：目标店铺 zhanggui-test.myshopify.com；
真实回执样本（浏览器会话实测，非本包代码跑出）：
测试草稿 Zhanggui Test Draft，ID
 gid://shopify/Product/15464707915850，状态 DRAFT。

设计原则：
- 业务层只认这个接口，不认 Shopify / eBay 等任何一家平台。
  换平台时只加一个新实现文件，不改确认、台账、幂等逻辑。
- 只建草稿（draft），永远不直接上架。这是硬规则，写死在实现里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class ShopReceipt:
    """一次成功写入的回执。调用方把它和台账一起存下来。"""

    shop_product_id: str          # 店铺返回的产品 id，作为「写进去了」的证据
    status: str                   # 正常应为 "draft"，且必须经读回确认
    url: str | None = None        # 店铺后台能点开的链接（有就填）
    operation_id: str = ""        # 发请求前生成的稳定操作 ID（见 WriteIntent）
    payload_sha256: str = ""      # 本次发送内容的摘要，供核对时比对
    raw: dict = field(default_factory=dict)  # 平台原始返回，排障用


@dataclass(frozen=True)
class WriteIntent:
    """一次外部写入的意图。发请求「之前」就要持久化到台账。

    作用：远端建成功、本地回执丢了 / 进程崩了之后，恢复时凭它
    去核对，而不是凭记忆再建一次。operation_id 必须稳定、
    全局唯一（建议 uuid4），同一任务 + 同一版本只能有一个。

    注意：frozen 只冻住字段引用，不冻住 tags 列表本身。
    调用方传进来之前就应把 tags 复制成一份快照；适配器
    收到后也不得原地改 intent.tags，拼包时一律先复制。
    """

    operation_id: str             # 稳定操作 ID，全局唯一
    task_id: str                  # 本机 Listing 任务编号
    draft_version: str            # 要写入的草稿版本号
    store_domain: str             # 目标店铺域名，必须绑定测试店
    title: str
    body_html: str
    vendor: str
    tags: list[str]
    payload_sha256: str           # title/body/vendor/tags 拼包后的摘要
    idempotency_key: str          # 台账层幂等键，与 operation_id 一一对应


@dataclass(frozen=True)
class ReconcileResult:
    """核对结果。只分四种，没有第五种。"""

    status: Literal["found", "not_found", "unknown", "conflict"]
    product: dict | None = None   # found 时是远端商品；其余时一般为空
    reason: str = ""              # 给人看的说明：凭什么下这个结论


class ShopAdapter(Protocol):
    """店铺写入适配器。所有方法只抛 ShopError 家族异常。"""

    def create_draft_product(
        self,
        *,
        intent: WriteIntent,
    ) -> ShopReceipt:
        """在店铺里建一个草稿产品，返回回执。

        约定：
        - intent 里的 title / body_html 必须已经是「只用证据原句
          拼出来的草稿」，适配器不做任何模型改写、不补资料里
          没有的内容。
        - 发请求前调用方必须已持久化 intent；适配器先校验
          intent（店铺、操作 ID、摘要、任务 / 版本 / 幂等键），
          校验不过直接抛 ShopError，不发请求。
        - 适配器收到 intent 后先把完整 operation_id 记成远端
          可查询的关联标记再创建，这样回执丢了也能凭标记找回。
        - 超时、5xx、响应解析失败、或返回里看不清到底建没建时，
          必须抛 ShopUnknownError，让调用方走 reconcile() 核对，
          绝不能在这里自动再调一次 create。
        - 返回前必须确认远端状态真是 DRAFT，且必须做一次
          独立读回确认（mutation 返回 + 随后 get_product 读回，
          两次确认分别记录在回执 raw 里）；读回失败是
          ShopUnknownError，读回状态不是 DRAFT 是
          ShopConflictError。
        - 已有商品 ID 但状态不是 DRAFT：抛 ShopConflictError，
          不是 ShopError——远端已有副作用，禁止当“没写进去”
          重试，禁止自动删除或发布来补偿。
        """
        ...

    def get_product(self, shop_product_id: str) -> dict | None:
        """按店铺产品 id 查一个产品。

        三态约定：
        - 查到：返回商品 dict；
        - 响应里明确 product: null：返回 None；
        - 端点 404、401 / 403 / 5xx、响应缺 product 字段、
          解析失败：抛 ShopUnknownError，调用方只能维持
          「查询未知」，绝不能把 None 当成不存在去重建。
        """
        ...

    def reconcile(self, intent: WriteIntent) -> ReconcileResult:
        """凭 WriteIntent 核对「到底建没建」。

        - found：远端有且仅有一件与 intent 匹配的商品（完整
          操作标记 + 标题 / 厂商 + 正文 + 内容摘要都对得上），
          返回该商品；
        - not_found：只在「创建请求明确未发出」的前提下才可
          返回（例如发前核对）；可靠地查过、远端确实没有
          匹配商品时，可以重试；
        - unknown：查询本身失败 / 超时 / 响应缺字段 / 一次
          空搜索（已发出但结果不明时，索引可能未可见），
          维持未知，禁止重建；调用方应持久化核对次数、
          时间与证据，安排受控复查或人工核对；
        - conflict：找到多件、或标题对上但操作标记 / 正文 /
          摘要对不上、或状态不是 DRAFT，需人工处理，
          禁止自动重建或自动删除。
        """
        ...


class ShopError(Exception):
    """店铺调用失败（明确没写进去、无副作用，可按业务规则决定是否重试）。

    语义：只有「明确没写进去」才能用它。已有商品、
    已有副作用、状态不明，都不属于它。
    """


class ShopConflictError(ShopError):
    """远端已有商品 / 已有副作用，但不合只建草稿的目标。

    收到它时禁止自动重建、禁止自动删除或发布来补偿，
    只能人工核对，保留远端 ID 与可排查信息。
    """


class ShopNotFoundError(ShopError):
    """明确不存在。只用于 get_product() 里响应明确 product: null
    的语义；端点 404 不属于它（端点问题是 ShopUnknownError）。"""


class ShopUnknownError(ShopError):
    """状态不明：不知道到底写没写进去。

    调用方收到这个异常时只能走 reconcile() 核对，禁止自动重写。
    """
