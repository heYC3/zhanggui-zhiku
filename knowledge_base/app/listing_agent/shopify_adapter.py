"""Shopify GraphQL 适配器：指定开发店、仅建 DRAFT、独立读回与严格核对。

当前实际路径为 legacy 静态 Admin Token，client credentials 保留作迁移。
超时与解析异常不重试创建；完整操作标记、正文、原始 tags 用于核对。
配置与真实验收范围见 docs/shopify.md；模拟测试不能替代真实联调。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:  # 包内导入（落盘后的正常路径）
    from .shop_adapter import (
        ReconcileResult,
        ShopAdapter,
        ShopConflictError,
        ShopError,
        ShopUnknownError,
        ShopReceipt,
        WriteIntent,
    )
except ImportError:  # 平铺文件独立测试时的回退路径
    from shop_adapter import (  # type: ignore[no-redef]
        ReconcileResult,
        ShopAdapter,
        ShopConflictError,
        ShopError,
        ShopUnknownError,
        ShopReceipt,
        WriteIntent,
    )

API_VERSION = "2026-10"
_GRAPHQL_PATH = f"/admin/api/{API_VERSION}/graphql.json"
_TOKEN_PATH = "/admin/oauth/access_token"
_TIMEOUT_S = 15
_TOKEN_REFRESH_MARGIN_S = 300  # 提前 5 分钟刷新 24 小时 Token
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _shopify_proxies():
    transport = os.getenv("LISTING_SHOPIFY_TRANSPORT", "direct").strip().lower()
    if transport == "direct":
        return {}
    if transport != "pythonanywhere":
        raise ShopError("不支持的 Shopify 出网方式")
    proxy = urllib.request.getproxies().get("https", "")
    parsed = urllib.parse.urlsplit(proxy)
    if (parsed.scheme != "http" or parsed.hostname != "proxy.server" or parsed.port != 3128
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ShopError("PythonAnywhere 出网仅允许官方 proxy.server:3128")
    return {"https": proxy}


def _urlopen_direct(req, *, timeout):
    # 默认直连；仅显式选择 PythonAnywhere 时使用其官方代理。拒绝重定向。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(_shopify_proxies()), _NoRedirect())
    return opener.open(req, timeout=timeout)


def _op_tag(operation_id: str) -> str:
    """远端可查询的操作标记：完整 operation_id，不截断。

    完整 operation_id，不截断：前 12 位相同的两个 ID
    不能撞成同一个 tag；完整标记才是一次操作的身份。
    """
    return f"zhanggui-op-{operation_id}"


def _normalize_body_html(body_html: str) -> str:
    """正文规范化：只消除空白差异，不改内容。

    Shopify 可能对 descriptionHtml 做空白规范化，核对时
    允许这种差异；内容本身（文字、标签、顺序）必须一致。
    """
    text = (body_html or "").strip()
    text = re.sub(r">\s+<", "><", text)
    text = re.sub(r">\s+", ">", text)
    text = re.sub(r"\s+<", "<", text)
    text = re.sub(r"\s+", " ", text)
    return text


def load_env_file(path: str | None = None) -> dict[str, str]:
    """加载 .env 文件进 os.environ，不覆盖已有变量。

    查找顺序：显式 path → 环境变量 SHOPIFY_ENV_FILE →
    knowledge_base/.env（项目已有 load_project_env() 的
    指向）→ 项目根目录 .env。找到第一个存在的文件就停。
    返回本次实际载入的键值；文件不存在返回 {}。
    注意：四行 Shopify 配置应合并进已有 .env，不要用
    四行文件直接覆盖已有模型、数据库等配置。
    """
    if os.environ.get("PYTHON_DOTENV_DISABLED", "").lower() in {"1", "true", "yes"}:
        return {}
    candidates: list[str] = []
    if path:
        candidates.append(path)
    if os.environ.get("SHOPIFY_ENV_FILE"):
        candidates.append(os.environ["SHOPIFY_ENV_FILE"])
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "..", "..", ".env"))  # knowledge_base/.env
    candidates.append(os.path.join(here, "..", "..", "..", ".env"))
    loaded: dict[str, str] = {}
    for cand in candidates:
        cand = os.path.normpath(cand)
        if not os.path.isfile(cand):
            continue
        with open(cand, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
                    loaded[key] = value
        return loaded
    return loaded


def build_draft_payload(title: str, body_html: str, vendor: str, tags: list[str]) -> dict:
    """拼出 GraphQL ProductCreateInput。不联网，可在本机单测。

    create_draft_product() 必须调它，不许另拼一份。
    """
    return {
        "title": title,
        "descriptionHtml": body_html,
        "vendor": vendor,
        "tags": list(tags),
        "status": "DRAFT",
    }


# 兼容旧名：build_product_input 是 build_draft_payload 的别名，同样返回 GraphQL 输入。
build_product_input = build_draft_payload


class ShopifyAdapter(ShopAdapter):
    def __init__(
        self,
        store_domain: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        legacy_admin_token: str | None = None,
    ) -> None:
        self.store_domain = store_domain or os.environ.get("SHOPIFY_STORE_DOMAIN", "")
        self.client_id = client_id or os.environ.get("SHOPIFY_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("SHOPIFY_CLIENT_SECRET", "")
        # legacy 是当前实际主路；client credentials 是后续迁移。
        self._legacy_token = legacy_admin_token or os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
        if not self.store_domain:
            raise ShopError("缺 SHOPIFY_STORE_DOMAIN，先在 .env 里配好（不要写进代码、不要提交）。")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", self.store_domain):
            raise ShopError("SHOPIFY_STORE_DOMAIN 必须是合法的 myshopify.com 店铺域名")
        if self._legacy_token:
            self.auth_mode = "legacy"
        elif self.client_id and self.client_secret:
            self.auth_mode = "client_credentials"
        elif self.client_id or self.client_secret:
            # 仅有 client_id、没有 client_secret 时直接报错，
            # 不要留到换 Token 时才失败。
            raise ShopError("SHOPIFY_CLIENT_ID 与 SHOPIFY_CLIENT_SECRET 必须成对提供，缺一个就不能走 client credentials。")
        else:
            raise ShopError(
                "缺凭证：要么 SHOPIFY_ADMIN_TOKEN（当前实际主路：legacy 静态 Token），"
                "要么 SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET（Dev Dashboard 路线）。"
            )
        if self.auth_mode == "legacy":
            logging.getLogger(__name__).info("正在使用 legacy 静态 Admin Token（当前实际主路，"
                  "仅限开发店 zhanggui-test 的应用 Zhanggui Zhiku Listing Agent）。"
                  "Dev Dashboard 验证通了再迁 client credentials grant。")
        self.base = f"https://{self.store_domain}"
        self._token_lock = threading.Lock()
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ---- 发送前校验：不过校验就不发请求 ----

    def _validate_intent(self, intent: WriteIntent) -> list[str]:
        """校验 WriteIntent，返回 tags 的一份副本。

        校验不过抛 ShopError（明确没写进去、无副作用）。
        台账唯一性与并发占用由业务层实现，这里只做输入校验。
        """
        if not isinstance(intent.operation_id, str) or not _OPERATION_ID_RE.match(intent.operation_id):
            raise ShopError("WriteIntent.operation_id 非法：必须非空，只含字母/数字/_/-，最长 64 字符。")
        if intent.store_domain != self.store_domain:
            raise ShopError(
                f"WriteIntent.store_domain 与适配器目标店铺不一致："
                f"intent={intent.store_domain!r}，适配器={self.store_domain!r}，禁止跨店发送。"
            )
        for name in ("task_id", "draft_version", "idempotency_key"):
            if not isinstance(getattr(intent, name), str) or not getattr(intent, name).strip():
                raise ShopError(f"WriteIntent.{name} 不能为空。")
        for name in ("title", "body_html", "vendor"):
            if not isinstance(getattr(intent, name), str) or not getattr(intent, name).strip():
                raise ShopError(f"WriteIntent.{name} 不能为空。")
        if not isinstance(intent.tags, list) or not all(isinstance(t, str) for t in intent.tags):
            raise ShopError("WriteIntent.tags 必须是字符串列表。")
        expected_sha = _payload_sha256(intent.title, intent.body_html, intent.vendor, list(intent.tags))
        if intent.payload_sha256 != expected_sha:
            raise ShopError("WriteIntent.payload_sha256 与内容重算不一致：内容在持久化后被改过，禁止发送，先核对台账。")
        return list(intent.tags)

    # ---- Token：当前主路 legacy；没有它时才走 client credentials ----

    def _get_token(self) -> str:
        if self._legacy_token:
            # 当前实际主路：已建的 legacy custom 应用的静态 Token。
            return self._legacy_token
        now = time.time()
        if self._token and now < self._token_expires_at - _TOKEN_REFRESH_MARGIN_S:
            return self._token
        with self._token_lock:
            now = time.time()
            if self._token and now < self._token_expires_at - _TOKEN_REFRESH_MARGIN_S:
                return self._token
            body = urllib.parse.urlencode(
                {
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                }
            ).encode()
            req = urllib.request.Request(
                self.base + _TOKEN_PATH,
                method="POST",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with _urlopen_direct(req, timeout=_TIMEOUT_S) as resp:
                    data = json.loads(resp.read() or b"{}")
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                raise ShopUnknownError(f"换取 Shopify Token 失败，状态不明：{e}") from e
            except (json.JSONDecodeError, UnicodeDecodeError, http.client.IncompleteRead) as e:
                raise ShopUnknownError(f"换取 Shopify Token 时响应解析失败，状态不明：{e}") from e
            token = data.get("access_token")
            if not token:
                raise ShopUnknownError(f"换取 Shopify Token 未返回 access_token：{data}")
            expires_in = int(data.get("expires_in") or 86399)
            self._token = str(token)
            self._token_expires_at = time.time() + expires_in
            return self._token

    # ---- 内部：一次 GraphQL 调用，区分「明确失败」和「状态不明」----

    def _graphql(self, query: str, variables: dict | None = None) -> dict:
        token = self._get_token()
        req = urllib.request.Request(
            self.base + _GRAPHQL_PATH,
            method="POST",
            data=json.dumps({"query": query, "variables": variables or {}}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": token,
            },
        )
        try:
            with _urlopen_direct(req, timeout=_TIMEOUT_S) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            # GraphQL 端点 404 是端点/域名/路径/版本问题，
            # 不是某个商品不存在，不能转 ShopNotFoundError。
            if e.code == 404:
                raise ShopUnknownError(f"Shopify GraphQL 端点 404（域名/路径/版本问题），状态不明：{body}") from e
            if 400 <= e.code < 500 and e.code not in {408, 429}:
                raise ShopError(f"Shopify 明确拒绝 HTTP {e.code}：{body}") from e
            raise ShopUnknownError(f"Shopify HTTP {e.code}，状态不明，需 reconcile 核对：{body}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise ShopUnknownError(f"调用超时或断网，状态不明，需 reconcile 核对：{e}") from e
        except http.client.IncompleteRead as e:
            raise ShopUnknownError(f"响应只读到部分内容，状态不明，需 reconcile 核对：{e}") from e
        try:
            data = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ShopUnknownError(f"响应不是合法 JSON，状态不明，需 reconcile 核对：{e}") from e
        if not isinstance(data, dict):
            raise ShopUnknownError(f"Shopify 响应不是 JSON 对象，状态不明：{data!r}")
        if data.get("errors"):
            # 顶层 GraphQL errors：不知道 mutation 执行到哪一步，按未知处理。
            raise ShopUnknownError(f"Shopify GraphQL 顶层 errors，状态不明：{data['errors']}")
        if "data" not in data:
            raise ShopUnknownError(f"Shopify 响应缺 data 字段，状态不明：{data}")
        if not isinstance(data["data"], dict):
            raise ShopUnknownError("Shopify 响应 data 不是对象，状态不明")
        return data["data"]

    # ---- 接口实现 ----

    def create_draft_product(self, *, intent: WriteIntent) -> ShopReceipt:
        # 先校验 intent，不过校验不发请求；tags 先复制，不改 intent。
        tags = self._validate_intent(intent)
        # 必须复用 build_draft_payload()，不许另拼一份。
        payload = build_draft_payload(intent.title, intent.body_html, intent.vendor, tags)
        # 远端可查询标记：完整 operation_id，供回执丢失时 reconcile 找回。
        payload["tags"] = [*tags, _op_tag(intent.operation_id)]
        mutation = """
        mutation productCreate($product: ProductCreateInput!) {
          productCreate(product: $product) {
            product { id status title vendor }
            userErrors { field message }
          }
        }
        """
        data = self._graphql(mutation, {"product": payload})
        result = data.get("productCreate")
        if not isinstance(result, dict) or not isinstance(result.get("userErrors"), list):
            raise ShopUnknownError("productCreate 响应缺少明确业务结果，状态不明")
        user_errors = result["userErrors"]
        if user_errors:
            if result.get("product"):
                raise ShopUnknownError("productCreate 同时返回 product 和 userErrors，需核对副作用")
            # 业务明确拒绝：没建进去，可以修好再试。
            raise ShopError(f"Shopify productCreate 明确拒绝：{user_errors}")
        product = result.get("product") or {}
        gid = str(product.get("id") or "")
        if not gid:
            raise ShopUnknownError(f"Shopify 返回里没有产品 id，状态不明：{data}")
        status = product.get("status")
        if status is None:
            # 状态缺失是未知，不是明确失败也不是成功。
            raise ShopUnknownError(f"Shopify 返回里没有状态字段，状态不明：{data}")
        if status != "DRAFT":
            # 已有商品 ID 但状态不是 DRAFT——远端已有副作用，
            # 抛 ShopConflictError，不再归入“明确没写进去”的 ShopError。
            raise ShopConflictError(
                f"Shopify 返回状态不是 DRAFT（status={status!r}），远端已有商品 {gid}，"
                f"需人工核对，禁止自动重建/删除/发布：{data}"
            )
        # 独立读回确认。mutation 返回 + 随后 get_product 读回，
        # 两次确认分别记进回执 raw；读回失败是未知，读回不符是冲突。
        try:
            readback = self.get_product(gid)
        except ShopUnknownError as e:
            raise ShopUnknownError(f"创建返回 DRAFT 但读回确认失败，状态不明：{e}") from e
        if readback is None:
            raise ShopUnknownError(f"创建返回 DRAFT 但读回查不到商品 {gid}，状态不明。")
        if not readback.get("status") or not readback.get("id") or "title" not in readback or "vendor" not in readback:
            raise ShopUnknownError("创建读回响应缺必要字段，结果未知")
        if readback.get("status") != "DRAFT":
            raise ShopConflictError(
                f"创建返回 DRAFT 但读回状态是 {readback.get('status')!r}，商品 {gid} 需人工核对。"
            )
        if readback["id"] != gid:
            raise ShopConflictError("创建返回与独立读回的商品 ID 不一致，需人工核对")
        if readback.get("title") != intent.title or readback.get("vendor") != intent.vendor:
            raise ShopConflictError(
                f"创建返回 DRAFT 但读回标题/厂商对不上，商品 {gid} 需人工核对：{readback}"
            )
        numeric_id = gid.rsplit("/", 1)[-1]
        return ShopReceipt(
            shop_product_id=numeric_id,
            status="DRAFT",
            url=f"https://{self.store_domain}/admin/products/{numeric_id}",
            operation_id=intent.operation_id,
            payload_sha256=intent.payload_sha256,
            raw={
                "product": product,
                "readback": readback,
                "mutation_confirmed": True,
                "readback_confirmed": True,
                "idempotency_key": intent.idempotency_key,
            },
        )

    def get_product(self, shop_product_id: str) -> dict | None:
        # shop_product_id 可能是数字 id，也可能是 gid；统一转成 gid。
        gid = shop_product_id if shop_product_id.startswith("gid://") else f"gid://shopify/Product/{shop_product_id}"
        query = """
        query getProduct($id: ID!) {
          product(id: $id) { id status title vendor }
        }
        """
        try:
            data = self._graphql(query, {"id": gid})
        except ShopError as e:
            # 401/403 等查询失败一律转查询未知，不逃逸 ShopError。
            raise ShopUnknownError(f"查询商品 {gid} 失败，状态不明：{e}") from e
        # 只有响应里明确 product: null 才返回 None；
        # 缺 product 字段是响应结构异常，按未知处理。
        if "product" not in data:
            raise ShopUnknownError(f"查询商品 {gid} 的响应缺 product 字段，状态不明：{data}")
        product = data.get("product")
        if product is None:
            return None
        if not isinstance(product, dict) or not product.get("id"):
            raise ShopUnknownError("查询商品响应结构异常，结果未知")
        return product

    def reconcile(self, intent: WriteIntent, *, already_sent: bool = True) -> ReconcileResult:
        """凭操作标记 + 正文 + 摘要核对到底建没建。

        already_sent=True（默认）：创建已发出但结果不明。
        一次空搜索返回 unknown，禁止自动重建；调用方应持久化
        核对次数、时间与证据，安排受控复查或人工核对。
        already_sent=False：发前核对，空搜索才可返回 not_found。
        """
        self._validate_intent(intent)
        query = """
        query productsByTag($query: String!) {
          products(first: 5, query: $query) {
            edges { node { id status title vendor tags descriptionHtml } }
          }
        }
        """
        try:
            data = self._graphql(query, {"query": f"tag:{_op_tag(intent.operation_id)}"})
        except ShopError as e:
            # 401/403 等核对查询失败一律 unknown，不逃逸。
            return ReconcileResult(status="unknown", reason=f"核对查询本身失败，维持未知：{e}")
        # 缺 products 字段是响应结构异常，不是空搜索结果。
        if "products" not in data or not isinstance(data.get("products"), dict):
            return ReconcileResult(status="unknown", reason=f"核对响应缺 products 字段，维持未知：{data}")
        edges = data["products"].get("edges")
        if edges is None:
            return ReconcileResult(status="unknown", reason="核对响应缺 edges 字段，维持未知。")
        if not isinstance(edges, list) or any(not isinstance(e, dict) or not isinstance(e.get("node"), dict) for e in edges):
            return ReconcileResult(status="unknown", reason="核对响应 edges 结构异常，维持未知。")
        nodes = [e.get("node") or {} for e in edges]
        if not nodes:
            if already_sent:
                return ReconcileResult(
                    status="unknown",
                    reason="按操作标记查过一次、结果为空，但创建已发出、索引可能未可见，"
                           "一次空搜索不能证明未创建，禁止自动重建；请受控复查或人工核对。",
                )
            return ReconcileResult(status="not_found", reason="发前核对：按操作标记查过，远端没有匹配商品，可以发送。")

        required = {"id", "status", "title", "vendor", "tags", "descriptionHtml"}
        if any(not required.issubset(node) or not node.get("id") or not node.get("status")
               or not isinstance(node.get("tags"), list) or not all(isinstance(t, str) for t in node["tags"])
               for node in nodes):
            return ReconcileResult(status="unknown", reason="核对商品缺少必要字段，结果未知。")

        def _matches(node: dict) -> bool:
            # 操作标记、正文、原始 tags 都要对上，才算同一件商品。
            node_tags = node.get("tags") or []
            if _op_tag(intent.operation_id) not in node_tags:
                return False
            if node.get("title") != intent.title or node.get("vendor") != intent.vendor:
                return False
            if _normalize_body_html(str(node.get("descriptionHtml") or "")) != _normalize_body_html(intent.body_html):
                return False
            expected_tags = set(intent.tags)
            if not expected_tags.issubset(set(node_tags)):
                return False
            return True

        matches = [n for n in nodes if _matches(n)]
        if len(matches) == 1 and len(nodes) == 1:
            node = matches[0]
            if node.get("status") != "DRAFT":
                return ReconcileResult(status="conflict", product=node, reason=f"找到商品但状态不是 DRAFT（{node.get('status')!r}），需人工处理。")
            return ReconcileResult(status="found", product=node, reason="按完整操作标记、正文与摘要找到唯一匹配的 DRAFT 商品。")
        return ReconcileResult(
            status="conflict",
            product=nodes[0],
            reason=f"按操作标记找到 {len(nodes)} 件，完整核对匹配 {len(matches)} 件，需人工处理，禁止自动重建或删除。",
        )

    # reconcile_by_title 保留作兼容，返回三态结果，不再只取第一件。
    # 注意：它是发前按标题查找，不是已发出操作的核对；空搜索返回 not_found。
    def reconcile_by_title(self, title: str) -> ReconcileResult:
        query = """
        query productsByTitle($query: String!) {
          products(first: 5, query: $query) {
            edges { node { id status title vendor tags } }
          }
        }
        """
        try:
            data = self._graphql(query, {"query": f"title:{title}"})
        except ShopError as e:
            return ReconcileResult(status="unknown", reason=f"核对查询本身失败，维持未知：{e}")
        if "products" not in data or not isinstance(data.get("products"), dict):
            return ReconcileResult(status="unknown", reason="核对响应缺 products 字段，维持未知。")
        edges = data["products"].get("edges")
        if edges is None:
            return ReconcileResult(status="unknown", reason="核对响应缺 edges 字段，维持未知。")
        nodes = [e.get("node") or {} for e in edges]
        if not nodes:
            return ReconcileResult(status="not_found", reason="按标题查过，没有匹配商品。")
        matches = [n for n in nodes if n.get("title") == title]
        if len(matches) == 1 and len(nodes) == 1:
            return ReconcileResult(status="found", product=matches[0], reason="按标题找到唯一匹配商品（仍建议以操作标记核对为准）。")
        return ReconcileResult(status="conflict", product=nodes[0], reason=f"按标题找到 {len(nodes)} 件，匹配 {len(matches)} 件，需人工处理。")


def _payload_sha256(title: str, body_html: str, vendor: str, tags: list[str]) -> str:
    """拼包内容的摘要，供 WriteIntent.payload_sha256 用。

    注意：摘要算的是原始 body_html 与原始 tags（不含操作标记），
    与 _normalize_body_html 的核对是两回事：前者防持久化后
    内容被改，后者容忍平台空白规范化。
    """
    raw = json.dumps({"title": title, "body_html": body_html, "vendor": vendor, "tags": sorted(tags)}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()
