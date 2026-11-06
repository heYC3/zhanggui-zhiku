# -*- coding: utf-8 -*-
"""一次性生成开发集和冻结测集。生成后数据集以 json 为准。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def loc(sku: str, chunk: str) -> str:
    return f"listing_data/products/{sku}.json#{chunk}"


def retrieval(id_, split, qtype, query, sku, chunk, points, purpose):
    return {
        "id": id_,
        "split": split,
        "题型": qtype,
        "问题": query,
        "标准要点": points,
        "证据位置": loc(sku, chunk),
        "kind": "retrieval",
        "query": query,
        "sku": sku,
        "expect_chunk": chunk,
        "purpose": purpose,
    }


def draft(id_, split, query, sku, chunk, expect, forbid, points, purpose):
    return {
        "id": id_,
        "split": split,
        "题型": "草稿要点",
        "问题": query,
        "标准要点": points,
        "证据位置": loc(sku, chunk),
        "kind": "draft",
        "query": query,
        "sku": sku,
        "expect_chunk": chunk,
        "expect_in_text": expect,
        "forbid_in_text": forbid,
        "purpose": purpose,
    }


def workflow(id_, split, qtype, query, purpose, **extra):
    item = {
        "id": id_,
        "split": split,
        "题型": qtype,
        "问题": query,
        "标准要点": extra.pop("points"),
        "证据位置": extra.pop("where"),
        "kind": "workflow",
        "query": query,
        "purpose": purpose,
    }
    item.update(extra)
    return item


FORBID_X1 = ["防水", "降噪", "最"]
FORBID_PRO = ["防水", "最"]
FORBID_P20 = ["防水", "最"]

frozen = [
    retrieval(
        "paraphrase-battery",
        "frozen",
        "改写检索",
        "掌柜示例耳机 X1 单次充电能听多久",
        "zg-ear-x1",
        "zg-ear-x1-play",
        ["连续播放 30 小时"],
        "问法里没有「连续播放」，用来看 HyDE 是否比关键词多找回证据",
    ),
    retrieval(
        "keyword-bluetooth",
        "frozen",
        "关键词检索",
        "掌柜示例耳机 X1 的蓝牙版本",
        "zg-ear-x1",
        "zg-ear-x1-link",
        ["蓝牙版本是 5.3"],
        "问法里带了原文词，关键词本身就应该命中",
    ),
    retrieval(
        "pro-anc",
        "frozen",
        "型号区分",
        "掌柜示例耳机 X1 Pro 有没有主动降噪",
        "zg-ear-x1-pro",
        "zg-ear-x1-pro-anc",
        ["支持主动降噪"],
        "相近型号必须命中 Pro，不能拿 X1 的资料充数",
    ),
    retrieval("kw-x1-weight", "frozen", "关键词检索", "掌柜示例耳机 X1 单耳重量多少克", "zg-ear-x1", "zg-ear-x1-weight", ["单耳重量 4.8 克"], "问法带了重量和克"),
    retrieval("kw-x1-charge", "frozen", "关键词检索", "掌柜示例耳机 X1 充满需要多少分钟", "zg-ear-x1", "zg-ear-x1-charge", ["大约需要 40 分钟"], "问法带了充满和分钟"),
    retrieval("kw-x1-play", "frozen", "关键词检索", "掌柜示例耳机 X1 可以连续播放多久", "zg-ear-x1", "zg-ear-x1-play", ["连续播放 30 小时"], "问法带了连续播放"),
    retrieval("kw-pro-play", "frozen", "关键词检索", "掌柜示例耳机 X1 Pro 连续播放多久", "zg-ear-x1-pro", "zg-ear-x1-pro-play", ["连续播放 40 小时"], "Pro 的播放时间，不能写成 30 小时"),
    retrieval("kw-pro-weight", "frozen", "关键词检索", "掌柜示例耳机 X1 Pro 单耳重量", "zg-ear-x1-pro", "zg-ear-x1-pro-weight", ["单耳重量 5.2 克"], "问法带了单耳重量"),
    retrieval("kw-p20-cap", "frozen", "关键词检索", "掌柜示例移动电源 P20 电芯容量多少毫安时", "zg-power-p20", "zg-power-p20-capacity", ["电芯容量 20000 毫安时"], "问法带了电芯容量和毫安时"),
    retrieval("kw-p20-out", "frozen", "关键词检索", "掌柜示例移动电源 P20 单口输出多少瓦", "zg-power-p20", "zg-power-p20-output", ["单口输出 65 瓦"], "问法带了单口输出"),
    retrieval("para-x1-runtime", "frozen", "改写检索", "掌柜示例耳机 X1 续航多久", "zg-ear-x1", "zg-ear-x1-play", ["连续播放 30 小时"], "用续航代替连续播放"),
    retrieval("para-x1-day", "frozen", "改写检索", "掌柜示例耳机 X1 出门一天电够不够听歌", "zg-ear-x1", "zg-ear-x1-play", ["只统计听歌"], "口语问一天够不够"),
    retrieval("para-x1-charge", "frozen", "改写检索", "掌柜示例耳机 X1 没电了要充多久", "zg-ear-x1", "zg-ear-x1-charge", ["大约需要 40 分钟"], "没有说充满和分钟"),
    retrieval("para-x1-heavy", "frozen", "改写检索", "掌柜示例耳机 X1 戴着沉不沉", "zg-ear-x1", "zg-ear-x1-weight", ["单耳重量 4.8 克"], "没有说重量和克"),
    retrieval("para-x1-pair", "frozen", "改写检索", "掌柜示例耳机 X1 怎么和手机连上", "zg-ear-x1", "zg-ear-x1-link", ["打开盒盖就能和手机配对"], "没有说蓝牙"),
    retrieval("para-pro-listen", "frozen", "改写检索", "掌柜示例耳机 X1 Pro 能听多久", "zg-ear-x1-pro", "zg-ear-x1-pro-play", ["连续播放 40 小时"], "Pro 的口语续航"),
    retrieval("para-pro-subway", "frozen", "改写检索", "掌柜示例耳机 X1 Pro 坐地铁吵不吵", "zg-ear-x1-pro", "zg-ear-x1-pro-anc", ["支持主动降噪"], "没有说降噪"),
    retrieval("para-pro-heavier", "frozen", "改写检索", "掌柜示例耳机 X1 Pro 会不会更重", "zg-ear-x1-pro", "zg-ear-x1-pro-weight", ["比 X1 略重"], "没有说克"),
    retrieval("para-p20-laptop", "frozen", "改写检索", "掌柜示例移动电源 P20 能不能给笔记本充电", "zg-power-p20", "zg-power-p20-output", ["可以给笔记本充电"], "问能不能给笔记本充电"),
    retrieval("para-p20-power", "frozen", "改写检索", "掌柜示例移动电源 P20 电量有多大", "zg-power-p20", "zg-power-p20-capacity", ["电芯容量 20000 毫安时"], "没有说毫安时"),
    retrieval("para-p20-watt", "frozen", "改写检索", "掌柜示例移动电源 P20 功率高不高", "zg-power-p20", "zg-power-p20-output", ["单口输出 65 瓦"], "没有说瓦"),
    retrieval("para-x1-color", "frozen", "改写检索", "掌柜示例耳机 X1 有几种外观", "zg-ear-x1", "zg-ear-x1-weight", ["颜色有黑色和白色"], "外观改写颜色"),
    draft(
        "draft-paraphrase",
        "frozen",
        "掌柜示例耳机 X1 单次充电能听多久，写成亚马逊要点",
        "zg-ear-x1",
        "zg-ear-x1-play",
        "30",
        FORBID_X1,
        ["连续播放 30 小时"],
        "同一条改写问法，看草稿有没有写出资料里的 30 小时",
    ),
    draft("draft-x1-link", "frozen", "掌柜示例耳机 X1 的蓝牙版本，写成亚马逊要点", "zg-ear-x1", "zg-ear-x1-link", "5.3", FORBID_X1, ["蓝牙版本是 5.3"], "要点里要出现 5.3，不能编防水或降噪"),
    draft("draft-x1-weight", "frozen", "掌柜示例耳机 X1 单耳重量，写成亚马逊要点", "zg-ear-x1", "zg-ear-x1-weight", "4.8", FORBID_X1, ["单耳重量 4.8 克"], "要点里要出现 4.8 克"),
    draft("draft-pro-anc", "frozen", "掌柜示例耳机 X1 Pro 有没有主动降噪，写成亚马逊要点", "zg-ear-x1-pro", "zg-ear-x1-pro-anc", "主动降噪", FORBID_PRO, ["支持主动降噪"], "Pro 可以写降噪，不能写防水"),
    draft("draft-pro-play", "frozen", "掌柜示例耳机 X1 Pro 能听多久，写成亚马逊要点", "zg-ear-x1-pro", "zg-ear-x1-pro-play", "40", FORBID_PRO, ["连续播放 40 小时"], "不能把 Pro 写成 30 小时；要点里应有 40"),
    draft("draft-p20-out", "frozen", "掌柜示例移动电源 P20 单口输出，写成亚马逊要点", "zg-power-p20", "zg-power-p20-output", "65", FORBID_P20, ["单口输出 65 瓦"], "要点里要出现 65 瓦"),
    draft("draft-p20-cap", "frozen", "掌柜示例移动电源 P20 电芯容量，写成亚马逊要点", "zg-power-p20", "zg-power-p20-capacity", "20000", FORBID_P20, ["电芯容量 20000 毫安时"], "要点里要出现 20000"),
    workflow(
        "ambiguous-family",
        "frozen",
        "型号歧义",
        "掌柜示例耳机能听多久",
        "只说了系列，X1 和 X1 Pro 都像，必须先问清型号",
        points=["先确认是 X1 还是 X1 Pro，不能直接写草稿"],
        where="listing_data/products/zg-ear-x1.json 与 zg-ear-x1-pro.json 共用别名「掌柜示例耳机」",
        expect_status="ambiguous",
    ),
    workflow(
        "missing-product",
        "frozen",
        "无资料",
        "给星河无人机 Mini 写商品标题",
        "资料库里没有这个商品，不能硬写草稿",
        points=["资料不足，不写草稿"],
        where="listing_data/products 中不存在该型号",
        expect_status="insufficient",
    ),
    workflow(
        "pronoun-followup",
        "frozen",
        "指代续问",
        "把它写成亚马逊五点",
        "上一句已经点名 Pro，这句「它」不能改判成 X1",
        points=["草稿型号是 X1 Pro", "支持主动降噪"],
        where="listing_data/products/zg-ear-x1-pro.json#zg-ear-x1-pro-anc",
        history=["我在看掌柜示例耳机 X1 Pro"],
        expect_sku="zg-ear-x1-pro",
    ),
    workflow(
        "ambiguous-color",
        "frozen",
        "型号歧义",
        "掌柜示例耳机有几种外观",
        "只说系列，两款耳机都能对上，先问清型号",
        points=["先确认型号"],
        where="listing_data/products/zg-ear-x1.json 与 zg-ear-x1-pro.json 共用别名",
        expect_status="ambiguous",
    ),
    workflow(
        "missing-lamp",
        "frozen",
        "无资料",
        "给北欧台灯 L2 写商品标题",
        "资料库没有台灯",
        points=["资料不足，不写草稿"],
        where="listing_data/products 中不存在该型号",
        expect_status="insufficient",
    ),
    workflow(
        "missing-watch",
        "frozen",
        "无资料",
        "手表 S3 续航多久",
        "名字像自编品牌，但资料里没有手表",
        points=["资料不足，不写草稿"],
        where="listing_data/products 中不存在该型号",
        expect_status="insufficient",
    ),
    workflow(
        "pronoun-p20",
        "frozen",
        "指代续问",
        "它单口多少瓦，写成要点",
        "上一句点名 P20，这句「它」不能改判成耳机",
        points=["单口输出 65 瓦"],
        where="listing_data/products/zg-power-p20.json#zg-power-p20-output",
        history=["我在看掌柜示例移动电源 P20"],
        expect_sku="zg-power-p20",
    ),
    workflow(
        "pronoun-x1",
        "frozen",
        "指代续问",
        "它多重，写成要点",
        "上一句点名 X1，这句「它」不能改判成 Pro",
        points=["单耳重量 4.8 克"],
        where="listing_data/products/zg-ear-x1.json#zg-ear-x1-weight",
        history=["我在看掌柜示例耳机 X1"],
        expect_sku="zg-ear-x1",
    ),
    workflow(
        "ambiguous-brand",
        "frozen",
        "型号歧义",
        "掌柜示例能用多久",
        "品牌名同时对上耳机和移动电源，必须先问清型号",
        points=["先确认型号"],
        where="三个商品共用别名「掌柜示例」",
        expect_status="ambiguous",
    ),
]

dev_specs = [
    ("dev-kw-x1-play-a", "关键词检索", "掌柜示例耳机 X1 音乐可以连续播放多少小时", "zg-ear-x1", "zg-ear-x1-play", ["连续播放 30 小时"]),
    ("dev-kw-x1-play-b", "关键词检索", "掌柜示例耳机 X1 听歌能连续播放多久", "zg-ear-x1", "zg-ear-x1-play", ["只统计听歌"]),
    ("dev-kw-x1-link-a", "关键词检索", "掌柜示例耳机 X1 蓝牙版本是多少", "zg-ear-x1", "zg-ear-x1-link", ["蓝牙版本是 5.3"]),
    ("dev-kw-x1-link-b", "关键词检索", "掌柜示例耳机 X1 打开盒盖怎么配对", "zg-ear-x1", "zg-ear-x1-link", ["打开盒盖就能和手机配对"]),
    ("dev-kw-x1-weight-a", "关键词检索", "掌柜示例耳机 X1 单耳重量", "zg-ear-x1", "zg-ear-x1-weight", ["单耳重量 4.8 克"]),
    ("dev-kw-x1-weight-b", "关键词检索", "掌柜示例耳机 X1 有黑色和白色吗", "zg-ear-x1", "zg-ear-x1-weight", ["颜色有黑色和白色"]),
    ("dev-kw-x1-charge-a", "关键词检索", "掌柜示例耳机 X1 从没电到充满要多久", "zg-ear-x1", "zg-ear-x1-charge", ["大约需要 40 分钟"]),
    ("dev-kw-x1-charge-b", "关键词检索", "掌柜示例耳机 X1 充满大约需要几分钟", "zg-ear-x1", "zg-ear-x1-charge", ["40 分钟"]),
    ("dev-kw-pro-play-a", "关键词检索", "掌柜示例耳机 X1 Pro 音乐可以连续播放多久", "zg-ear-x1-pro", "zg-ear-x1-pro-play", ["连续播放 40 小时"]),
    ("dev-kw-pro-play-b", "型号区分", "掌柜示例耳机 X1 Pro 听歌不含通话能播多久", "zg-ear-x1-pro", "zg-ear-x1-pro-play", ["不含通话"]),
    ("dev-kw-pro-anc-a", "关键词检索", "掌柜示例耳机 X1 Pro 支持主动降噪吗", "zg-ear-x1-pro", "zg-ear-x1-pro-anc", ["支持主动降噪"]),
    ("dev-kw-pro-anc-b", "关键词检索", "掌柜示例耳机 X1 Pro 嘈杂环境里能不能听清", "zg-ear-x1-pro", "zg-ear-x1-pro-anc", ["嘈杂环境里也能听清"]),
    ("dev-kw-pro-weight-a", "关键词检索", "掌柜示例耳机 X1 Pro 单耳多少克", "zg-ear-x1-pro", "zg-ear-x1-pro-weight", ["单耳重量 5.2 克"]),
    ("dev-kw-pro-weight-b", "型号区分", "掌柜示例耳机 X1 Pro 是不是比 X1 略重", "zg-ear-x1-pro", "zg-ear-x1-pro-weight", ["比 X1 略重"]),
    ("dev-kw-p20-cap-a", "关键词检索", "掌柜示例移动电源 P20 电芯容量", "zg-power-p20", "zg-power-p20-capacity", ["电芯容量 20000 毫安时"]),
    ("dev-kw-p20-cap-b", "关键词检索", "掌柜示例移动电源 P20 标注容量以什么为准", "zg-power-p20", "zg-power-p20-capacity", ["标注容量以电芯为准"]),
    ("dev-kw-p20-out-a", "关键词检索", "掌柜示例移动电源 P20 单口输出", "zg-power-p20", "zg-power-p20-output", ["单口输出 65 瓦"]),
    ("dev-kw-p20-out-b", "关键词检索", "掌柜示例移动电源 P20 可以给笔记本充电吗", "zg-power-p20", "zg-power-p20-output", ["可以给笔记本充电"]),
    ("dev-para-x1-play-a", "改写检索", "掌柜示例耳机 X1 听歌能撑多久", "zg-ear-x1", "zg-ear-x1-play", ["连续播放 30 小时"]),
    ("dev-para-x1-play-b", "改写检索", "掌柜示例耳机 X1 一次充满后能用几个小时", "zg-ear-x1", "zg-ear-x1-play", ["连续播放 30 小时"]),
    ("dev-para-x1-link-a", "改写检索", "掌柜示例耳机 X1 连手机麻不麻烦", "zg-ear-x1", "zg-ear-x1-link", ["打开盒盖就能和手机配对"]),
    ("dev-para-x1-link-b", "改写检索", "掌柜示例耳机 X1 用的是哪一代无线连接", "zg-ear-x1", "zg-ear-x1-link", ["蓝牙版本是 5.3"]),
    ("dev-para-x1-weight-a", "改写检索", "掌柜示例耳机 X1 一只耳朵有多重", "zg-ear-x1", "zg-ear-x1-weight", ["单耳重量 4.8 克"]),
    ("dev-para-x1-weight-b", "改写检索", "掌柜示例耳机 X1 卖哪些颜色", "zg-ear-x1", "zg-ear-x1-weight", ["颜色有黑色和白色"]),
    ("dev-para-x1-charge-a", "改写检索", "掌柜示例耳机 X1 从关机到又能听要等多久", "zg-ear-x1", "zg-ear-x1-charge", ["大约需要 40 分钟"]),
    ("dev-para-x1-charge-b", "改写检索", "掌柜示例耳机 X1 充电快不快", "zg-ear-x1", "zg-ear-x1-charge", ["大约需要 40 分钟"]),
    ("dev-para-pro-play-a", "改写检索", "掌柜示例耳机 X1 Pro 听歌能撑多久", "zg-ear-x1-pro", "zg-ear-x1-pro-play", ["连续播放 40 小时"]),
    ("dev-para-pro-play-b", "型号区分", "掌柜示例耳机 X1 Pro 会不会比普通款更能听", "zg-ear-x1-pro", "zg-ear-x1-pro-play", ["连续播放 40 小时"]),
    ("dev-para-pro-anc-a", "改写检索", "掌柜示例耳机 X1 Pro 地铁里说话听得见吗", "zg-ear-x1-pro", "zg-ear-x1-pro-anc", ["嘈杂环境里也能听清"]),
    ("dev-para-pro-anc-b", "改写检索", "掌柜示例耳机 X1 Pro 有没有消掉环境声", "zg-ear-x1-pro", "zg-ear-x1-pro-anc", ["支持主动降噪"]),
    ("dev-para-pro-weight-a", "改写检索", "掌柜示例耳机 X1 Pro 一只有多重", "zg-ear-x1-pro", "zg-ear-x1-pro-weight", ["单耳重量 5.2 克"]),
    ("dev-para-pro-weight-b", "型号区分", "掌柜示例耳机 X1 Pro 戴久了会不会比 X1 累", "zg-ear-x1-pro", "zg-ear-x1-pro-weight", ["比 X1 略重"]),
    ("dev-para-p20-cap-a", "改写检索", "掌柜示例移动电源 P20 能存多少电", "zg-power-p20", "zg-power-p20-capacity", ["电芯容量 20000 毫安时"]),
    ("dev-para-p20-cap-b", "改写检索", "掌柜示例移动电源 P20 容量数字看哪一个", "zg-power-p20", "zg-power-p20-capacity", ["标注容量以电芯为准"]),
    ("dev-para-p20-out-a", "改写检索", "掌柜示例移动电源 P20 能不能带电脑", "zg-power-p20", "zg-power-p20-output", ["可以给笔记本充电"]),
    ("dev-para-p20-out-b", "改写检索", "掌柜示例移动电源 P20 一个口够不够快充", "zg-power-p20", "zg-power-p20-output", ["单口输出 65 瓦"]),
    ("dev-kw-x1-play-c", "关键词检索", "掌柜示例耳机 X1 不含通话能连续播放多久", "zg-ear-x1", "zg-ear-x1-play", ["不含通话"]),
    ("dev-para-pro-anc-c", "型号区分", "掌柜示例耳机 X1 Pro 吵的时候还听得清吗", "zg-ear-x1-pro", "zg-ear-x1-pro-anc", ["嘈杂环境里也能听清"]),
]

dev = []
for spec in dev_specs:
    dev.append(
        retrieval(spec[0], "dev", spec[1], spec[2], spec[3], spec[4], spec[5], "开发集检索，不进入对外基线")
    )

dev_drafts = [
    ("dev-draft-x1-play", "掌柜示例耳机 X1 听歌能撑多久，写成亚马逊要点", "zg-ear-x1", "zg-ear-x1-play", "30", FORBID_X1, ["连续播放 30 小时"]),
    ("dev-draft-x1-link", "掌柜示例耳机 X1 怎么和手机连上，写成亚马逊要点", "zg-ear-x1", "zg-ear-x1-link", "5.3", FORBID_X1, ["蓝牙版本是 5.3"]),
    ("dev-draft-x1-charge", "掌柜示例耳机 X1 没电了要充多久，写成亚马逊要点", "zg-ear-x1", "zg-ear-x1-charge", "40", FORBID_X1, ["大约需要 40 分钟"]),
    ("dev-draft-x1-color", "掌柜示例耳机 X1 有几种外观，写成亚马逊要点", "zg-ear-x1", "zg-ear-x1-weight", "黑色", FORBID_X1, ["颜色有黑色和白色"]),
    ("dev-draft-pro-play", "掌柜示例耳机 X1 Pro 听歌能撑多久，写成亚马逊要点", "zg-ear-x1-pro", "zg-ear-x1-pro-play", "40", FORBID_PRO, ["连续播放 40 小时"]),
    ("dev-draft-pro-weight", "掌柜示例耳机 X1 Pro 一只有多重，写成亚马逊要点", "zg-ear-x1-pro", "zg-ear-x1-pro-weight", "5.2", FORBID_PRO, ["单耳重量 5.2 克"]),
    ("dev-draft-pro-anc", "掌柜示例耳机 X1 Pro 地铁里能不能听清，写成亚马逊要点", "zg-ear-x1-pro", "zg-ear-x1-pro-anc", "主动降噪", FORBID_PRO, ["支持主动降噪"]),
    ("dev-draft-p20-cap", "掌柜示例移动电源 P20 能存多少电，写成亚马逊要点", "zg-power-p20", "zg-power-p20-capacity", "20000", FORBID_P20, ["电芯容量 20000 毫安时"]),
    ("dev-draft-p20-out", "掌柜示例移动电源 P20 能不能给笔记本充电，写成亚马逊要点", "zg-power-p20", "zg-power-p20-output", "65", FORBID_P20, ["可以给笔记本充电"]),
    ("dev-draft-p20-cell", "掌柜示例移动电源 P20 容量数字看哪一个，写成亚马逊要点", "zg-power-p20", "zg-power-p20-capacity", "电芯", FORBID_P20, ["标注容量以电芯为准"]),
    ("dev-draft-x1-weight", "掌柜示例耳机 X1 一只耳朵有多重，写成亚马逊要点", "zg-ear-x1", "zg-ear-x1-weight", "4.8", FORBID_X1, ["单耳重量 4.8 克"]),
    ("dev-draft-pro-heavier", "掌柜示例耳机 X1 Pro 会不会更重，写成亚马逊要点", "zg-ear-x1-pro", "zg-ear-x1-pro-weight", "略重", FORBID_PRO, ["比 X1 略重"]),
]
for spec in dev_drafts:
    dev.append(draft(spec[0], "dev", spec[1], spec[2], spec[3], spec[4], spec[5], spec[6], "开发集草稿，不进入对外基线"))

dev_flows = [
    ("dev-amb-1", "型号歧义", "掌柜示例耳机多重", ["先确认是 X1 还是 X1 Pro"], "两个耳机共用「掌柜示例耳机」", {"expect_status": "ambiguous"}),
    ("dev-amb-2", "型号歧义", "掌柜示例耳机怎么连接", ["先确认型号"], "两个耳机共用别名", {"expect_status": "ambiguous"}),
    ("dev-amb-3", "型号歧义", "掌柜示例卖多少钱", ["先确认型号"], "三个商品共用「掌柜示例」", {"expect_status": "ambiguous"}),
    ("dev-miss-1", "无资料", "给星河耳机 Air 写标题", ["资料不足，不写草稿"], "listing_data/products 中不存在该型号", {"expect_status": "insufficient"}),
    ("dev-miss-2", "无资料", "键盘 K1 有没有蓝牙", ["资料不足，不写草稿"], "listing_data/products 中不存在该型号", {"expect_status": "insufficient"}),
    ("dev-miss-3", "无资料", "给露营灯 Mini 写五点", ["资料不足，不写草稿"], "listing_data/products 中不存在该型号", {"expect_status": "insufficient"}),
    ("dev-pro-1", "指代续问", "这个写成亚马逊要点", ["草稿型号是 X1 Pro"], "listing_data/products/zg-ear-x1-pro.json", {"history": ["我在看掌柜示例耳机 X1 Pro"], "expect_sku": "zg-ear-x1-pro"}),
    ("dev-pro-2", "指代续问", "它有没有降噪，写成要点", ["支持主动降噪"], "listing_data/products/zg-ear-x1-pro.json#zg-ear-x1-pro-anc", {"history": ["刚才看的是掌柜示例耳机 X1 Pro"], "expect_sku": "zg-ear-x1-pro"}),
    ("dev-pro-3", "指代续问", "这款能听多久，写成要点", ["连续播放 30 小时"], "listing_data/products/zg-ear-x1.json#zg-ear-x1-play", {"history": ["我在看掌柜示例耳机 X1"], "expect_sku": "zg-ear-x1"}),
    ("dev-pro-4", "指代续问", "上面那个容量多少，写成要点", ["电芯容量 20000 毫安时"], "listing_data/products/zg-power-p20.json#zg-power-p20-capacity", {"history": ["我在看移动电源 P20"], "expect_sku": "zg-power-p20"}),
    ("dev-amb-4", "型号歧义", "掌柜示例耳机充满要多久", ["先确认型号"], "两个耳机共用别名", {"expect_status": "ambiguous"}),
    ("dev-miss-4", "无资料", "给儿童手表 Q2 写标题", ["资料不足，不写草稿"], "listing_data/products 中不存在该型号", {"expect_status": "insufficient"}),
]
for spec in dev_flows:
    dev.append(
        workflow(
            spec[0],
            "dev",
            spec[1],
            spec[2] if False else spec[2],
            "开发集流程题，不进入对外基线",
            points=spec[3],
            where=spec[4],
            **spec[5],
        )
    )
    dev[-1]["问题"] = spec[2]
    dev[-1]["query"] = spec[2]


def dump(name, items, extra):
    payload = {
        "frozen_on": "2026-11-05",
        "corpus": "listing_data/products 自编 3 个商品，synthetic，不是在售商品",
        "fields": ["问题", "标准要点", "证据位置", "题型"],
        "note": extra,
        "tasks": items,
    }
    (ROOT / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


required = ["问题", "标准要点", "证据位置", "题型"]
for group in (frozen, dev):
    ids = [item["id"] for item in group]
    if len(ids) != len(set(ids)):
        raise SystemExit("id 重复")
    for item in group:
        for key in required:
            if key not in item or item[key] in ("", [], None):
                raise SystemExit(f"缺字段 {item['id']} {key}")

dump("frozen_test.json", frozen, "冻结测集。对外基线只报这一份，不要拿开发集调完再改这里的题。")
dump("dev.json", dev, "开发集。可以用来看失败例子，不进入对外基线表。")
index = {
    "frozen_on": "2026-11-05",
    "corpus": "listing_data/products 自编 3 个商品，synthetic，不是在售商品",
    "fields": required,
    "note": "开发集和冻结测集已拆开。对外基线只跑 frozen_test.json。",
    "dev": "dev.json",
    "frozen_test": "frozen_test.json",
    "counts": {"dev": len(dev), "frozen_test": len(frozen), "total": len(dev) + len(frozen)},
}
(ROOT / "tasks.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(index["counts"], ensure_ascii=False))
