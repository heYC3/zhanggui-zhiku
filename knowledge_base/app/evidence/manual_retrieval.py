"""手册检索的文本视图与候选配额；不改写作为证据返回的原文。"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from html.parser import HTMLParser


class _ManualText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.rows = None
        self.row = []
        self.cell = None
        self.span = (1, 1)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table':
            if self.rows is not None:
                self.handle_endtag('table')
            self.rows = []
        elif tag == 'tr':
            if self.rows is None:
                self.rows = []
            self.row = []
        elif tag in ('td', 'th'):
            if self.rows is None:
                self.rows = []
            self.cell = []
            def span(name):
                try:
                    return min(1000, max(1, int(attrs.get(name, 1))))
                except (ValueError, TypeError):
                    return 1
            self.span = (span('rowspan'), span('colspan'))
        elif tag in ('br', 'p', 'div', 'li'):
            (self.cell if self.cell is not None else self.parts).append('\n')

    def handle_data(self, data):
        (self.cell if self.cell is not None else self.parts).append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append((''.join(self.cell).strip(), *self.span))
            self.cell = None
        elif tag == 'tr' and self.rows is not None:
            self.rows.append(self.row)
            self.row = []
        elif tag == 'table' and self.rows is not None:
            if self.cell is not None:
                self.handle_endtag('td')
            if self.row:
                self.rows.append(self.row)
                self.row = []
            carried = {}
            for row in self.rows:
                values = {column: text for column, (text, _) in carried.items()}
                upcoming = {column: (text, n-1) for column, (text, n) in carried.items() if n > 1}
                column = 0
                for text, rowspan, colspan in row:
                    while any(column+i in values for i in range(colspan)):
                        column += 1
                    for i in range(colspan):
                        values[column+i] = text
                        if rowspan > 1:
                            upcoming[column+i] = (text, rowspan-1)
                    column += colspan
                self.parts.append('\n' + ' | '.join(values.get(i, '') for i in range(max(values, default=-1)+1)) + '\n')
                carried = upcoming
            self.rows = None
        elif tag in ('p', 'div', 'li'):
            self.parts.append('\n')

    def close(self):
        super().close()
        # 原始切片可能在HTML表格中间截断；仍保留已经见到的单元格，不补造缺失值。
        if self.rows is not None:
            self.handle_endtag('table')


def search_text(content: str) -> str:
    """去掉图片URL和图片转述，展开表格跨行单元格；保留表后注释及操作原文。"""
    text = re.sub(r'!\[[\s\S]*?\]\([^\n]*?\)', '\n', content or '')
    parser = _ManualText()
    parser.feed(text)
    parser.close()
    return '\n'.join(re.sub(r'[ \t]+', ' ', line).strip() for line in ''.join(parser.parts).splitlines() if line.strip())


def lexical_terms(text: str) -> list[str]:
    text = unicodedata.normalize('NFKC', text).lower()
    terms = []
    for token in re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', text):
        if '\u4e00' <= token[0] <= '\u9fff' and len(token) > 1:
            terms.extend(token[i:i+2] for i in range(len(token)-1))
        else:
            terms.append(token)
    return terms


def lexical_rank(query: str, docs: list[dict], limit: int = 12) -> list[dict]:
    """按当前产品完整候选中的词频和逆文档频率补充召回，无答案词表。"""
    query_terms = set(lexical_terms(query))
    if not docs or not query_terms or limit <= 0:
        return []
    counters = [Counter(lexical_terms(search_text(doc.get('content') or doc.get('text') or ''))) for doc in docs]
    lengths = [sum(counts.values()) for counts in counters]
    average = max(1.0, sum(lengths)/len(lengths))
    frequencies = Counter(term for counts in counters for term in query_terms if term in counts)
    scores = []
    for index, (counts, length) in enumerate(zip(counters, lengths)):
        score = 0.0
        for term in query_terms.intersection(counts):
            idf = math.log(1 + (len(docs)-frequencies[term]+0.5)/(frequencies[term]+0.5))
            tf = counts[term]
            score += idf * tf * 2.2/(tf + 1.2*(0.4+0.6*length/average))
        if score > 0:
            scores.append((score, index))
    scores.sort(key=lambda pair: (-pair[0], pair[1]))
    return [docs[index] for _, index in scores[:limit]]


def document_key(doc: dict) -> tuple[str, str]:
    product = doc.get('item_name') or doc.get('product_id') or ''
    cid = doc.get('chunk_id')
    if cid is None or cid == '':
        cid = hashlib.sha256((doc.get('content') or doc.get('text') or '').encode('utf-8')).hexdigest()
    return product, str(cid)


def fuse_rankings(rankings: list[list[dict]], limit: int | None = None) -> list[dict]:
    scores, docs = {}, {}
    for ranking in rankings:
        seen = set()
        for rank, doc in enumerate(ranking, 1):
            key = document_key(doc)
            if key in seen:
                continue
            seen.add(key)
            docs.setdefault(key, doc)
            scores[key] = scores.get(key, 0.0) + 1/(60+rank)
    ordered = sorted(docs, key=lambda key: -scores[key])
    return [docs[key] for key in (ordered if limit is None else ordered[:limit])]


def candidate_union(rankings: list[list[dict]], limit: int) -> list[dict]:
    """为语义与词语召回各保留少量候选，避免新证据被融合前再次挤掉。"""
    if limit <= 0:
        return []
    active = [ranking for ranking in rankings if ranking]
    ranked = fuse_rankings(active)
    reserve = min(3, limit//max(1, len(active)))
    protected = {document_key(doc) for ranking in active for doc in ranking[:reserve]}
    selected = [doc for doc in ranked if document_key(doc) in protected][:limit]
    selected_keys = {document_key(doc) for doc in selected}
    for doc in ranked:
        if len(selected) >= limit:
            break
        if document_key(doc) not in selected_keys:
            selected.append(doc)
            selected_keys.add(document_key(doc))
    return [doc for doc in ranked if document_key(doc) in selected_keys]


def fuse_by_product(rankings: list[list[dict]], products: list[str], per_product: int = 12) -> list[dict]:
    """每个产品独立融合，再交替排列，名额和排名均不依赖产品输入顺序。"""
    names = list(dict.fromkeys(products))
    if not names:
        return fuse_rankings(rankings, per_product)
    groups = [fuse_rankings([[doc for doc in ranking if document_key(doc)[0] == name]
                            for ranking in rankings], per_product) for name in names]
    return [group[index] for index in range(max(map(len, groups), default=0)) for group in groups if index < len(group)]


def score_original_documents(query: str, docs: list[dict], reranker) -> list[dict]:
    """重排只使用可读视图；输出保留完整正文、产品身份和原始chunk_id。"""
    scores = reranker(query, [search_text(doc.get('content') or '') for doc in docs])
    if len(scores) != len(docs) or any(not math.isfinite(float(score)) for score in scores):
        raise ValueError('重排分数未完整覆盖候选文档')
    return sorted([{**doc, 'score': float(score)} for doc, score in zip(docs, scores)],
                  key=lambda doc: -doc['score'])


def score_document_passages(query: str, docs: list[dict], reranker) -> list[dict]:
    """复合问题同时评分完整文档和相关行窗口，缓解长规格表的主题稀释。"""
    views, owners = [], []
    for index, doc in enumerate(docs):
        text = search_text(doc.get('content') or '')
        candidates = [{'content': '\n'.join(lines[max(0, i-1):i+2])}
                      for lines in [text.splitlines()] for i in range(len(lines))]
        passages = lexical_rank(query, candidates, limit=2) if len(text) > 600 else []
        for view in dict.fromkeys([text, *(p['content'] for p in passages)]):
            views.append(view)
            owners.append(index)
    if not views:
        return []
    scores = reranker(query, views)
    if len(scores) != len(views) or any(not math.isfinite(float(s)) for s in scores):
        raise ValueError('重排分数未完整覆盖文档窗口')
    best = {}
    for index, score in zip(owners, scores):
        best[index] = max(best.get(index, float('-inf')), float(score))
    return sorted([{**doc, 'score': best[index]} for index, doc in enumerate(docs)],
                  key=lambda doc: -doc['score'])


def interleave_products(ranked: list[dict], products: list[str]) -> list[dict]:
    """按产品内相关性排序后交替交付；保留每个产品的原始分数。"""
    names = list(dict.fromkeys(products))
    groups = [[doc for doc in ranked if document_key(doc)[0] == name] for name in names]
    covered = {document_key(doc) for group in groups for doc in group}
    return ([group[index] for index in range(max(map(len, groups), default=0))
             for group in groups if index < len(group)] +
            [doc for doc in ranked if document_key(doc) not in covered])


def cover_products(selected: list[dict], ranked: list[dict], products: list[str], limit: int = 10) -> list[dict]:
    """截断后先保留每个已召回产品的最强证据，再填充其余高分证据。"""
    winners = []
    for product in dict.fromkeys(products):
        winner = next((doc for doc in ranked if document_key(doc)[0] == product), None)
        if winner is not None:
            winners.append(winner)
    result, seen = [], set()
    for doc in winners + selected:
        key = document_key(doc)
        if key not in seen and len(result) < limit:
            result.append(doc)
            seen.add(key)
    return result
