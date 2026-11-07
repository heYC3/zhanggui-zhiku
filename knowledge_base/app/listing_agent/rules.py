"""平台规则校验。网页确认和 MCP 工具都调用这一份。"""

from __future__ import annotations

import json
import re

from app.listing_agent.catalog import evidence_text_by_sku, get_product
from app.listing_agent.paths import PLATFORM_DIR


class Actor:
    """谁在调用。editor 才能确认写入，viewer 只能看和校验。"""

    def __init__(self, actor_id: str, role: str):
        self.actor_id = (actor_id or "").strip()
        self.role = (role or "").strip().lower()

    def require(self, allowed: tuple[str, ...]) -> None:
        if not self.actor_id or self.role not in allowed:
            allowed_text = "、".join(allowed)
            raise PermissionError(
                f"当前身份不能做这件事。需要 {allowed_text}，并且要带上操作者名字。"
            )


def load_rules(platform: str) -> dict:
    path = PLATFORM_DIR / f"{platform}.json"
    if not path.exists():
        raise LookupError(f"还没有「{platform}」的规则样例")
    return json.loads(path.read_text(encoding="utf-8"))


def _numbers_outside_model(title: str, model: str) -> list[str]:
    stripped = title.replace(model, "")
    return re.findall(r"\d+", stripped)


def validate_listing_draft(draft: dict, platform: str, actor: Actor) -> dict:
    """检查草稿是否越出证据和样例规则。viewer 与 editor 都可以调用。"""
    actor.require(("editor", "viewer"))
    rules = load_rules(platform)
    errors: list[str] = []
    title = (draft.get("title") or "").strip()
    bullets = [item.strip() for item in (draft.get("bullets") or []) if str(item).strip()]
    brand = (draft.get("brand") or "").strip()
    model = (draft.get("model") or "").strip()
    sku = draft.get("sku") or ""
    product = get_product(sku)
    evidence = evidence_text_by_sku().get(sku, "")
    joined = title + "\n" + "\n".join(bullets)

    if product is None:
        errors.append("草稿里的型号不在自编资料中")
    else:
        if brand != product["brand"]:
            errors.append("品牌和资料不一致")
        if model != product["model"]:
            errors.append("型号和资料不一致")

    if "title" in rules["required_fields"] and not title:
        errors.append("标题是空的")
    if len(title) > int(rules["title_max_chars"]):
        errors.append(f"标题超过 {rules['title_max_chars']} 个字")
    if not (int(rules["bullet_min"]) <= len(bullets) <= int(rules["bullet_max"])):
        errors.append(
            f"要点需要 {rules['bullet_min']} 到 {rules['bullet_max']} 条，现在是 {len(bullets)} 条"
        )

    for word in rules["forbidden_substrings"]:
        if word and word in joined:
            errors.append(f"出现了规则不允许的说法：{word}")

    if product is not None:
        # 标题只允许“品牌型号 + 资料原句”，阻止无数字的虚构属性混入标题。
        compact = lambda value: re.sub(r"\s+", "", value)
        identity = compact(product["brand"] + product["model"])
        title_compact = compact(title)
        fact = title_compact[len(identity):] if title_compact.startswith(identity) else None
        if fact is None or (fact and fact not in compact(evidence)):
            errors.append("标题必须由品牌型号和资料原句组成")
        for number in _numbers_outside_model(title, product["model"]):
            if number not in evidence:
                errors.append(f"标题里的数字 {number} 在资料中找不到")
        for bullet in bullets:
            if bullet not in evidence:
                errors.append("有一条要点不是资料原句")
                break

    return {
        "ok": not errors,
        "platform": platform,
        "errors": errors,
        "rules_name": rules["display_name"],
    }
