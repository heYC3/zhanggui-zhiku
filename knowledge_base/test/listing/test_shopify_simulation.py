"""模拟故障测试（不联网，不用真实 Token）。

跑法（本机）：
    python -m unittest discover -s test/listing -p 'test_shopify_simulation.py' -v

覆盖：
- _op_tag() 用完整 operation_id，前 12 位相同的两个 ID
  生成不同 tag；reconcile() 节点缺操作标记 / 正文不符
  时返回 conflict，不再算 found。
- reconcile() 空搜索：已发出（默认）返回 unknown，禁止
  重建；发前核对（already_sent=False）才返回 not_found；
  响应缺 products 字段返回 unknown。
- get_product()：端点 404、401、响应缺 product 字段一律
  抛 ShopUnknownError；只有明确 product: null 才返回 None。
- create()：返回 ACTIVE 抛 ShopConflictError（不是 ShopError）；
  状态缺失抛 ShopUnknownError；成功时必须做独立读回确认，
  读回失败抛 ShopUnknownError。
- _validate_intent()：跨店、摘要不匹配、operation_id 为空、
  任务/版本/幂等键缺失时抛 ShopError 且不发请求；tags
  列表不被原地修改。
- 凭证校验：仅有 client_id、没有 client_secret 时初始化报错。
- _payload_sha256()：内容变化时摘要变化（不只是同一输入
  与自身比较）。

本文件只做模拟，不联网。2026-11-08 的本机真实联调证据见 docs/shopify.md。
"""
import io
import json
import os
import urllib.request
from pathlib import Path
from unittest.mock import patch


from app.listing_agent.shop_adapter import (
    ReconcileResult,
    ShopConflictError,
    ShopError,
    ShopUnknownError,
    WriteIntent,
)
from app.listing_agent.shopify_adapter import (
    ShopifyAdapter,
    _normalize_body_html,
    _op_tag,
    _payload_sha256,
    build_draft_payload,
)


def _intent(**overrides):
    base = dict(
        operation_id="op-1234567890abcdef",
        task_id="task-1",
        draft_version="draft-1",
        store_domain="zhanggui-test.myshopify.com",
        title="掌柜示例耳机 X1",
        body_html="<p>续航 30 小时</p>",
        vendor="掌柜示例",
        tags=["耳机"],
        payload_sha256=_payload_sha256("掌柜示例耳机 X1", "<p>续航 30 小时</p>", "掌柜示例", ["耳机"]),
        idempotency_key="idem-1",
    )
    base.update(overrides)
    return WriteIntent(**base)


def _adapter():
    return ShopifyAdapter(store_domain="zhanggui-test.myshopify.com", legacy_admin_token="shpat_dummy")


def _node(intent, **overrides):
    node = {
        "id": "gid://shopify/Product/123",
        "status": "DRAFT",
        "title": intent.title,
        "vendor": intent.vendor,
        "tags": [*intent.tags, _op_tag(intent.operation_id)],
        "descriptionHtml": intent.body_html,
    }
    node.update(overrides)
    return node


def test_op_tag_full_identity():
    assert _op_tag("op-1234567890abcdef") == "zhanggui-op-op-1234567890abcdef"
    a = _op_tag("op-1234567890aaaa")
    b = _op_tag("op-1234567890bbbb")
    assert a != b, "前 12 位相同也不能撞成同一个 tag"
    print("ok _op_tag 用完整 operation_id，前 12 位相同也不撞")


def test_legacy_token_is_current_path():
    ad = _adapter()
    assert ad._get_token() == "shpat_dummy"
    print("ok _get_token 有 legacy Token 时直接返回，不走 client credentials")
    ad2 = ShopifyAdapter(
        store_domain="zhanggui-test.myshopify.com",
        client_id="cid_dummy", client_secret="csec_dummy",
        legacy_admin_token="shpat_dummy",
    )
    assert ad2._get_token() == "shpat_dummy"
    print("ok legacy 与 client_id 同时在时 legacy 优先")
    old = {k: os.environ.pop(k, None) for k in ("SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET", "SHOPIFY_ADMIN_TOKEN")}
    try:
        try:
            ShopifyAdapter(store_domain="zhanggui-test.myshopify.com")
        except ShopError:
            print("ok 缺 Client ID 且缺 legacy Token 时初始化报错")
        else:
            raise AssertionError("缺凭证应抛 ShopError")
        try:
            ShopifyAdapter(store_domain="zhanggui-test.myshopify.com", client_id="cid_dummy")
        except ShopError:
            print("ok 仅有 client_id、没有 client_secret 时初始化报错")
        else:
            raise AssertionError("仅有 client_id 应抛 ShopError")
    finally:
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v
    try:
        ShopifyAdapter(store_domain="", legacy_admin_token="shpat_dummy")
    except ShopError:
        print("ok 缺 SHOPIFY_STORE_DOMAIN 时初始化报错")
    else:
        raise AssertionError("缺域名应抛 ShopError")


def test_build_payload_and_sha_changes():
    p = build_draft_payload("T", "<p>B</p>", "V", ["a", "b"])
    assert p["status"] == "DRAFT" and p["descriptionHtml"] == "<p>B</p>" and p["tags"] == ["a", "b"]
    same = _payload_sha256("T", "<p>B</p>", "V", ["a"])
    assert same == _payload_sha256("T", "<p>B</p>", "V", ["a"])
    assert _payload_sha256("T", "<p>B2</p>", "V", ["a"]) != same, "正文变了摘要必须变"
    assert _payload_sha256("T", "<p>B</p>", "V", ["a", "b"]) != same, "tags 变了摘要必须变"
    assert _normalize_body_html("<p>  续航\n 30 小时 </p>") == _normalize_body_html("<p>续航 30 小时</p>")
    print("ok build_draft_payload / sha 随内容变化 / 正文规范化只容忍空白")


def test_validate_intent():
    ad = _adapter()
    intent = _intent()
    assert ad._validate_intent(intent) == ["耳机"]
    assert intent.tags == ["耳机"], "校验不许原地改 intent.tags"
    cases = {
        "跨店": _intent(store_domain="other.myshopify.com"),
        "摘要不匹配": _intent(payload_sha256="0" * 64),
        "operation_id 为空": _intent(operation_id=""),
        "任务为空": _intent(task_id=""),
        "幂等键为空": _intent(idempotency_key=""),
    }
    for name, bad in cases.items():
        try:
            ad.create_draft_product(intent=bad)
        except ShopError:
            print(f"ok intent 校验：{name} 抛 ShopError")
        else:
            raise AssertionError(f"intent 校验：{name} 应抛 ShopError")
    # 校验不过时一次网络请求都不应发出
    with patch.object(ad, "_graphql", side_effect=AssertionError("不应发请求")):
        try:
            ad.create_draft_product(intent=_intent(store_domain="other.myshopify.com"))
        except ShopError:
            print("ok intent 校验不过时不发请求")
        else:
            raise AssertionError("校验不过应抛 ShopError")


def test_get_product_states():
    ad = _adapter()
    with patch.object(ad, "_graphql", side_effect=ShopUnknownError("timeout")):
        try:
            ad.get_product("123")
        except ShopUnknownError:
            print("ok get_product 超时抛 ShopUnknownError（不再吞成 None）")
        else:
            raise AssertionError("get_product 超时应抛 ShopUnknownError")
    # 端点 404、401 都不是“商品不存在”，一律查询未知
    for err in (ShopUnknownError("endpoint 404"), ShopError("HTTP 401")):
        with patch.object(ad, "_graphql", side_effect=err):
            try:
                ad.get_product("123")
            except ShopUnknownError:
                pass
            else:
                raise AssertionError(f"get_product {err} 应抛 ShopUnknownError")
    print("ok get_product 端点 404 / 401 一律抛 ShopUnknownError")
    with patch.object(ad, "_graphql", return_value={}):
        try:
            ad.get_product("123")
        except ShopUnknownError:
            print("ok get_product 响应缺 product 字段抛 ShopUnknownError")
        else:
            raise AssertionError("缺 product 字段应抛 ShopUnknownError")
    with patch.object(ad, "_graphql", return_value={"product": None}):
        assert ad.get_product("123") is None
        print("ok get_product 明确 product: null 才返回 None")
    with patch.object(ad, "_graphql", return_value={"product": {"id": "gid://shopify/Product/123", "status": "DRAFT"}}):
        assert ad.get_product("123")["status"] == "DRAFT"
        print("ok get_product 查到返回商品")


def test_create_states():
    ad = _adapter()
    intent = _intent()
    # userErrors：明确拒绝
    with patch.object(ad, "_graphql", return_value={"productCreate": {"product": None, "userErrors": [{"field": "title", "message": "too long"}]}}):
        try:
            ad.create_draft_product(intent=intent)
        except ShopError:
            print("ok create userErrors 按明确拒绝（ShopError）")
        else:
            raise AssertionError("userErrors 应抛 ShopError")
    # 缺 id：状态不明
    with patch.object(ad, "_graphql", return_value={"productCreate": {"product": {}, "userErrors": []}}):
        try:
            ad.create_draft_product(intent=intent)
        except ShopUnknownError:
            print("ok create 缺 id 抛 ShopUnknownError")
        else:
            raise AssertionError("缺 id 应抛 ShopUnknownError")
    # 状态缺失：状态不明，不是明确失败
    with patch.object(ad, "_graphql", return_value={"productCreate": {"product": {"id": "gid://shopify/Product/123"}, "userErrors": []}}):
        try:
            ad.create_draft_product(intent=intent)
        except ShopUnknownError:
            print("ok create 状态缺失抛 ShopUnknownError")
        else:
            raise AssertionError("状态缺失应抛 ShopUnknownError")
    # 返回 ACTIVE：有副作用，抛 ShopConflictError，不是 ShopError 的“没写进去”
    with patch.object(ad, "_graphql", return_value={"productCreate": {"product": {"id": "gid://shopify/Product/123", "status": "ACTIVE"}, "userErrors": []}}):
        try:
            ad.create_draft_product(intent=intent)
        except ShopConflictError:
            print("ok create 返回 ACTIVE 抛 ShopConflictError")
        else:
            raise AssertionError("ACTIVE 应抛 ShopConflictError")
    # 成功 DRAFT：必须复用 build_draft_payload、带完整操作标记、且做独立读回确认
    seen = {}
    calls = []
    def fake_graphql(query, variables=None):
        calls.append(query)
        if "productCreate" in query:
            seen["variables"] = variables
            return {"productCreate": {"product": {"id": "gid://shopify/Product/123", "status": "DRAFT", "title": intent.title}, "userErrors": []}}
        return {"product": {"id": "gid://shopify/Product/123", "status": "DRAFT", "title": intent.title, "vendor": intent.vendor}}
    with patch.object(ad, "_graphql", side_effect=fake_graphql):
        receipt = ad.create_draft_product(intent=intent)
        assert receipt.status == "DRAFT" and receipt.shop_product_id == "123"
        assert receipt.operation_id == intent.operation_id
        assert _op_tag(intent.operation_id) in seen["variables"]["product"]["tags"]
        assert seen["variables"]["product"]["descriptionHtml"] == intent.body_html
        assert receipt.raw["mutation_confirmed"] and receipt.raw["readback_confirmed"]
        assert len(calls) == 2, "成功路径应有 mutation + 读回两次调用"
        assert intent.tags == ["耳机"], "create 不许原地改 intent.tags"
        print("ok create 成功 DRAFT 给回执，复用拼包、带完整操作标记、做了读回确认")
    # 读回失败：状态不明
    def fake_graphql_readback_fail(query, variables=None):
        if "productCreate" in query:
            return {"productCreate": {"product": {"id": "gid://shopify/Product/123", "status": "DRAFT", "title": intent.title}, "userErrors": []}}
        raise ShopUnknownError("readback timeout")
    with patch.object(ad, "_graphql", side_effect=fake_graphql_readback_fail):
        try:
            ad.create_draft_product(intent=intent)
        except ShopUnknownError:
            print("ok create 读回失败抛 ShopUnknownError")
        else:
            raise AssertionError("读回失败应抛 ShopUnknownError")


def test_graphql_parse_failures():
    ad = _adapter()
    class BadResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"not-json{"
    with patch("app.listing_agent.shopify_adapter._urlopen_direct", return_value=BadResp()):
        try:
            ad._graphql("query { shop { name } }")
        except ShopUnknownError:
            print("ok _graphql 非 JSON 响应归入 ShopUnknownError")
        else:
            raise AssertionError("非 JSON 应抛 ShopUnknownError")
    import http.client
    class IncompleteResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): raise http.client.IncompleteRead(b"partial")
    with patch("app.listing_agent.shopify_adapter._urlopen_direct", return_value=IncompleteResp()):
        try:
            ad._graphql("query { shop { name } }")
        except ShopUnknownError:
            print("ok _graphql 响应不完整归入 ShopUnknownError")
        else:
            raise AssertionError("IncompleteRead 应抛 ShopUnknownError")


def test_reconcile_states():
    ad = _adapter()
    intent = _intent()
    node = _node(intent)
    with patch.object(ad, "_graphql", return_value={"products": {"edges": [{"node": node}]}}):
        r = ad.reconcile(intent)
        assert r.status == "found", r
        print("ok reconcile 操作标记+正文+摘要全对上才算 found")
    # 节点缺操作标记：冲突，不算 found
    bad_tag = _node(intent, tags=["耳机"])
    with patch.object(ad, "_graphql", return_value={"products": {"edges": [{"node": bad_tag}]}}):
        assert ad.reconcile(intent).status == "conflict"
        print("ok reconcile 节点缺操作标记返回 conflict")
    # 正文不同：冲突
    bad_body = _node(intent, descriptionHtml="<p>续航 99 小时</p>")
    with patch.object(ad, "_graphql", return_value={"products": {"edges": [{"node": bad_body}]}}):
        assert ad.reconcile(intent).status == "conflict"
        print("ok reconcile 正文不符返回 conflict")
    # 空白差异：容忍，仍算 found
    ws_body = _node(intent, descriptionHtml="<p>  续航\n 30 小时 </p>")
    with patch.object(ad, "_graphql", return_value={"products": {"edges": [{"node": ws_body}]}}):
        assert ad.reconcile(intent).status == "found"
        print("ok reconcile 正文仅空白差异仍算 found")
    # 空搜索：已发出默认 unknown，禁止重建
    with patch.object(ad, "_graphql", return_value={"products": {"edges": []}}):
        r = ad.reconcile(intent)
        assert r.status == "unknown", r
        print("ok reconcile 已发出时一次空搜索返回 unknown，不可重建")
    # 空搜索：发前核对才可 not_found
    with patch.object(ad, "_graphql", return_value={"products": {"edges": []}}):
        assert ad.reconcile(intent, already_sent=False).status == "not_found"
        print("ok reconcile 发前核对时空搜索返回 not_found")
    # 响应缺 products 字段：unknown，不是空结果
    with patch.object(ad, "_graphql", return_value={}):
        assert ad.reconcile(intent).status == "unknown"
        print("ok reconcile 响应缺 products 字段返回 unknown")
    # 查询失败（含 401 转成的 ShopError）：unknown，不逃逸
    with patch.object(ad, "_graphql", side_effect=ShopError("HTTP 401")):
        assert ad.reconcile(intent).status == "unknown"
        print("ok reconcile 查询 401 返回 unknown")
    with patch.object(ad, "_graphql", return_value={"products": {"edges": [{"node": node}, {"node": node}]}}):
        assert ad.reconcile(intent).status == "conflict"
        print("ok reconcile 多件冲突")


def test_missing_fields_remain_unknown():
    ad, intent = _adapter(), _intent()
    for node in ({}, _node(intent, status=None)):
        with patch.object(ad, "_graphql", return_value={"products": {"edges": [{"node": node}]}}):
            assert ad.reconcile(intent).status == "unknown"
    for edges in ({"bad": "shape"}, [None], [{"node": "bad"}]):
        with patch.object(ad, "_graphql", return_value={"products": {"edges": edges}}):
            assert ad.reconcile(intent).status == "unknown"


def test_throttle_and_graphql_errors_unknown():
    import urllib.error
    ad = _adapter()
    for code in (408, 429):
        error = urllib.error.HTTPError(ad.base, code, "temporary", {}, io.BytesIO(b"temporary"))
        with patch("app.listing_agent.shopify_adapter._urlopen_direct", side_effect=error):
            try:
                ad._graphql("mutation { placeholder }")
            except ShopUnknownError:
                pass
            else:
                raise AssertionError("超时和限流不能按明确未创建处理")
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"errors":[{"message":"internal error"}],"data":null}'
    with patch("app.listing_agent.shopify_adapter._urlopen_direct", return_value=Resp()):
        try:
            ad._graphql("mutation { placeholder }")
        except ShopUnknownError:
            pass
        else:
            raise AssertionError("HTTP 200 的顶层 errors 必须按未知处理")


def test_env_loading_disabled():
    import tempfile
    from app.listing_agent.shopify_adapter import load_env_file
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / ".env"
        path.write_text("SHOPIFY_TEST_ONLY=example\n", encoding="utf-8")
        with patch.dict(os.environ, {"PYTHON_DOTENV_DISABLED": "1"}):
            assert load_env_file(str(path)) == {}
            assert "SHOPIFY_TEST_ONLY" not in os.environ
        with patch.dict(os.environ, {"PYTHON_DOTENV_DISABLED": "0", "SHOPIFY_TEST_ONLY": "existing"}):
            load_env_file(str(path))
            assert os.environ["SHOPIFY_TEST_ONLY"] == "existing"


def test_readback_missing_status_is_unknown():
    ad, intent = _adapter(), _intent()
    mutation = {"productCreate": {"product": {"id": "gid://shopify/Product/123", "status": "DRAFT"}, "userErrors": []}}
    readback = {"product": {"id": "gid://shopify/Product/123", "title": intent.title, "vendor": intent.vendor}}
    with patch.object(ad, "_graphql", side_effect=[mutation, readback]):
        try:
            ad.create_draft_product(intent=intent)
        except ShopUnknownError:
            pass
        else:
            raise AssertionError("读回缺状态不能按冲突或成功处理")


def test_transport_does_not_forward_credentials_to_redirect():
    from app.listing_agent.shopify_adapter import _urlopen_direct, _NoRedirect
    req = urllib.request.Request("https://zhanggui-test.myshopify.com/example")
    with patch("urllib.request.build_opener") as build:
        _urlopen_direct(req, timeout=1)
        assert build.call_args.args[0].proxies == {}
        assert isinstance(build.call_args.args[1], _NoRedirect)
        assert build.call_args.args[1].redirect_request(req, None, 302, "", {}, "https://other.example") is None


def load_tests(loader, tests, pattern):
    import unittest
    def isolated(fn):
        def run():
            with patch.dict(os.environ):
                for key in list(os.environ):
                    if key.startswith("SHOPIFY_"):
                        os.environ.pop(key)
                fn()
        run.__name__ = fn.__name__
        return run
    return unittest.TestSuite(unittest.FunctionTestCase(isolated(fn)) for name, fn in globals().items()
                              if name.startswith("test_") and callable(fn))


if __name__ == "__main__":
    import unittest
    unittest.main()
