"""Regenerate documentation SVGs with Python's standard library.

Run from any directory: python docs/assets/render_diagrams.py
These are diagrams / record summaries, not screenshots or new evaluations.
"""
from html import escape
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FONT = "'Microsoft YaHei','Noto Sans CJK SC',sans-serif"


class SVG:
    def __init__(self, width, height, title, subtitle):
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
                      '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10Z" fill="#94a3b8"/></marker></defs>']
        self.rect(0, 0, width, height, '#f8fafc', radius=0)
        self.text(34, 45, title, 26, '#0f172a', bold=True)
        self.text(34, 76, subtitle, 14, '#475569')

    def rect(self, x, y, w, h, fill, stroke='none', radius=12):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}"/>')

    def text(self, x, y, text, size=16, color='#0f172a', bold=False):
        self.parts.append(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{700 if bold else 400}" fill="{color}">{escape(str(text))}</text>')

    def arrow(self, x, y1, y2):
        self.parts.append(f'<path d="M{x},{y1} V{y2}" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>')

    def save(self, filename):
        (HERE / filename).write_text('\n'.join(self.parts + ['</svg>']) + '\n', encoding='utf-8')


def architecture():
    s = SVG(1154, 875, '掌柜智库 · 从资料到可确认的执行', '实现结构示意 · 三条工作流与共享证据接口 · 2026-11-08')
    columns = [
        ('01  资料导入', '8000 /import.html', [
            ('PDF / Markdown', 'PDF 经 MinerU；Markdown 直接进入处理'),
            ('图片处理与正文切片', '图片原件 + 视觉摘要；本机哈希 URL'),
            ('识别产品与来源', '产品名、标题、原文与支持切片'),
            ('BGE-M3 编码', '正文和产品名生成稠密 / 稀疏向量'),
            ('Milvus 手册证据', '问答与 Listing 共用 evidence/store.py'),
            ('图片后端可选', '默认本机文件；MinIO 本轮未验收'),
        ]),
        ('02  RAG 问答', '8001 /chat.html', [
            ('确认型号与显式历史', '缺型号先澄清；多产品分别保留证据'),
            ('原问题 / HyDE 召回', '向量检索 + 产品范围内的词语召回'),
            ('RRF 融合与重排', '保留原文；展开长表格的相关行'),
            ('装入实际生成上下文', '单独记录 context_docs，区分候选'),
            ('模型流式回答', '返回正文依据与上下文中的图片'),
            ('可选联网分支', '默认关闭；手册验收不使用联网'),
        ]),
        ('03  Listing Agent', '8002 /listing.html', [
            ('确认商品与取证', '默认自编离线资料；共享证据接口'),
            ('生成受约束草稿', '原句模板 / 可选真实模型'),
            ('结构与规则校验', '来源原句、型号边界、缺口提示'),
            ('人工确认', '服务端鉴权、任务归属与草稿版本'),
            ('先持久化 WriteIntent', '本地台账 / Shopify GraphQL DRAFT'),
            ('独立读回与回执', '未知只核对；不自动重复创建'),
        ]),
    ]
    for col, (title, port, rows) in enumerate(columns):
        x = 34 + col * 374
        s.rect(x, 105, 338, 66, '#0f172a')
        s.text(x + 18, 134, title, 19, '#ffffff', True)
        s.text(x + 18, 156, port, 13, '#cbd5e1')
        for i, (label, desc) in enumerate(rows):
            y = 191 + i * 83
            s.rect(x, y, 338, 64, '#ffffff' if i % 2 == 0 else '#eaf2ff', '#cbd5e1')
            s.text(x + 16, y + 26, label, 17, bold=True)
            s.text(x + 16, y + 49, desc, 12, '#475569')
            if i < 5 and not (i == 4 and col < 2):
                s.arrow(x + 169, y + 65, y + 80)
    s.rect(34, 711, 1086, 130, '#fff7ed', '#fed7aa')
    s.text(54, 741, '验证与恢复边界', 18, '#9a3412', True)
    s.text(54, 770, '公开合成 100 题 = 开发 62 + 冻结 38；闭环计分 16 题，检索另计 22 题。', 15, '#7c2d12')
    s.text(54, 797, '私有手册 5 本 / 54 题；首次验收只取新 24 题。检查点、操作意图与台账使用 SQLite。', 15, '#7c2d12')
    s.text(54, 824, 'Shopify 只验指定开发店 DRAFT；故障窗口为离线模拟。完整 RAG 容器部署未作本轮验收。', 14, '#7c2d12')
    s.save('architecture.svg')


def evaluation():
    report = json.loads((ROOT / 'knowledge_base/listing_data/reports/strategy_report.json').read_text(encoding='utf-8'))
    retrieval = report['retrieval']
    sparse_hits = sum(row['strategies']['sparse']['hit_at_3'] for row in retrieval)
    rrf_hits = sum(row['strategies']['rrf_hyde']['hit_at_3'] for row in retrieval)
    assert (len(retrieval), sparse_hits, rrf_hits) == (22, 14, 22)
    s = SVG(1154, 640, '验证结果 · 各任务分别计分', '已有记录的可视化摘要，不是新增实验 · 2026-11-08')
    panels = [
        (34, '公开自编商品 · 离线闭环', '#1d4ed8', [
            ('11/16 → 16/16', '关键词基线 → 当前草稿 / 确认流程'),
            (f'{sparse_hits}/22 → {rrf_hits}/22', '独立的检索 hit@3 题组'),
            ('62 + 38 = 100', '开发题 + 冻结题；不称 100 道冻结题'),
        ]),
        (594, '私有手册 · 首次新题验收', '#0f766e', [
            ('23/24', '严格整题通过；显式选图 4/5'),
            ('19/20 · 20/20', '正文事实题的检索 hit@1 · hit@3'),
            ('45/47 · 47/47', '前三证据覆盖 · 实际上下文覆盖'),
        ]),
    ]
    for x, title, color, rows in panels:
        s.rect(x, 105, 526, 348, '#ffffff', '#cbd5e1')
        s.rect(x, 105, 526, 52, color)
        s.text(x + 20, 138, title, 20, '#ffffff', True)
        for i, (metric, label) in enumerate(rows):
            y = 202 + i * 96
            s.text(x + 23, y, metric, 30, color, True)
            s.text(x + 23, y + 29, label, 15, '#475569')
    s.rect(34, 478, 1086, 126, '#fff7ed', '#fed7aa')
    s.text(54, 508, '读数边界', 17, '#9a3412', True)
    s.text(54, 535, '左侧为 3 个自编商品、离线字块向量；多个组件共同变化，不能归因于单一算法。', 15, '#7c2d12')
    s.text(54, 562, '右侧为 5 本手册 / 54 题中的新 24 题；同一开发者借助 Codex 实现与核对，不是独立盲评。', 15, '#7c2d12')
    s.text(54, 589, '来源：listing_data/reports/baseline.md、strategy_report.json、docs/evaluation.md；不公开私有题目。', 13, '#7c2d12')
    s.save('evaluation.svg')


def mcp():
    record = json.loads((HERE / 'mcp_record_2026-11-08.json').read_text(encoding='utf-8'))
    steps = {step['step']: step for step in record['steps']}
    assert not steps['call']['is_error'] and steps['bad_arguments']['is_error']
    assert steps['process_exit']['returncode'] == 0
    s = SVG(1154, 650, 'MCP · 官方客户端真实调用记录', '调用记录摘要图，非终端截图 · mcp==2.3.0 · stdio NDJSON · 2026-11-08')
    rows = [
        ('01', 'initialize', 'zhanggui-listing 0.1.0 · protocol 2025-11-25', '初始化成功'),
        ('02', 'list_tools', 'lookup_product_evidence / validate_listing_draft', '发现 2 个工具'),
        ('03', 'call_tool', 'lookup_product_evidence · 自编 X1 蓝牙证据', 'is_error: false'),
        ('04', 'bad_arguments', '缺少必填 query，参数校验拒绝请求', 'is_error: true（预期）'),
        ('05', 'process_exit', '客户端会话结束，子进程正常退出', 'returncode: 0'),
    ]
    for i, (number, name, desc, result) in enumerate(rows):
        y = 108 + 87 * i
        s.rect(34, y, 1086, 69, '#ffffff', '#cbd5e1')
        s.text(54, y + 42, number, 25, '#2563eb', True)
        s.text(112, y + 28, name, 18, bold=True)
        s.text(112, y + 52, desc, 14, '#475569')
        s.text(851, y + 41, result, 16, '#0f766e', True)
    s.text(34, 585, '来源：docs/assets/mcp_record_2026-11-08.json；仅移除本机路径、PID 与 stderr，未改写调用结果。', 14, '#475569')
    s.text(34, 614, '本探针实际调用查证据工具并验证坏参数；不代表已在外部桌面客户端安装，也不执行店铺写入。', 14, '#475569')
    s.save('mcp.svg')


if __name__ == '__main__':
    architecture()
    evaluation()
    mcp()
