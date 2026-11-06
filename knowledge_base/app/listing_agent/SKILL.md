---
name: listing-rule-check
description: 检查或检索掌柜智库 Listing 草稿时使用。调用本项目的 MCP 工具，不直接改台账。
---

# Listing 规则检查

商品资料在 `knowledge_base/listing_data/products`。平台样例规则在 `listing_data/platforms/amazon.json`。

需要查证据或检查草稿时，启动：

```powershell
python -m app.listing_agent.mcp_server
```

工作目录是 `knowledge_base`。

## 工具

- `lookup_product_evidence`：参数 `query`、`actor_id`、`role`，可选 `sku` 和 `strategy`。`role` 只能是 `editor` 或 `viewer`。
- `validate_listing_draft`：参数 `draft`、`platform`、`actor_id`、`role`。和网页点确认前用的是同一段校验代码。

两个工具都不写入草稿。确认、拒绝、取消走网页 `http://127.0.0.1:8002/listing.html`。

没有 `actor_id` 或身份不对时，工具返回无权，不要改参数绕过去。
