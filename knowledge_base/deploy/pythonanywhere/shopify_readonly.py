"""从平台私有配置读取 Token，通过平台代理执行固定的 Shopify 只读查询。"""

import argparse
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

STORE = "zhanggui-test.myshopify.com"
API_VERSION = "2026-10"
QUERY = """query ZhangguiReadOnlyProbe {
  shop { myshopifyDomain }
  products(first: 3, query: "status:draft") { nodes { status } }
}"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def run(env_file: Path) -> dict:
    values = {}
    for line in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    if values.get("SHOPIFY_STORE_DOMAIN") != STORE or not values.get("SHOPIFY_ADMIN_TOKEN"):
        raise ValueError("测试店配置缺失或不匹配")
    request = urllib.request.Request(
        f"https://{STORE}/admin/api/{API_VERSION}/graphql.json",
        data=json.dumps({"query": QUERY}).encode(),
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": values["SHOPIFY_ADMIN_TOKEN"]},
        method="POST",
    )
    # 免费平台使用其官方环境代理；不跟随重定向，不使用浏览器转发服务。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(), NoRedirect())
    with opener.open(request, timeout=20) as response:
        payload = json.loads(response.read())
    data = payload.get("data") or {}
    if payload.get("errors") or (data.get("shop") or {}).get("myshopifyDomain") != STORE:
        raise ValueError("GraphQL 只读查询失败或店铺身份不匹配")
    products = data.get("products")
    if not isinstance(products, dict) or not isinstance(products.get("nodes"), list):
        raise ValueError("GraphQL 商品读回缺失")
    statuses = [item.get("status") for item in products["nodes"]]
    if any(status != "DRAFT" for status in statuses):
        raise ValueError("草稿查询返回了缺失或非 DRAFT 状态")
    proxy = urllib.request.getproxies().get("https", "")
    return {"ok": True, "read_only": True, "store_domain": STORE, "api_version": API_VERSION,
            "sample_count": len(statuses), "sample_statuses": statuses,
            "query_sha256": hashlib.sha256(QUERY.encode()).hexdigest(),
            "proxy_host": urllib.parse.urlsplit(proxy).hostname if proxy else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.env_file)
    except Exception as exc:
        # 不打印异常正文或响应正文，避免将认证信息混入控制台和日志。
        print(json.dumps({"ok": False, "read_only": True, "error_type": type(exc).__name__,
                          "http_status": exc.code if isinstance(exc, urllib.error.HTTPError) else None}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
