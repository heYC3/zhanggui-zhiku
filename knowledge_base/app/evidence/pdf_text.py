"""保留 PDF 封面原生文本，补偿版面解析遗漏；不从产品目录生成事实。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


def recover_cover_text(pdf_path: str | Path, markdown: str) -> tuple[str, dict]:
    from pypdf import PdfReader

    path = Path(pdf_path)
    reader = PdfReader(path)
    raw = (reader.pages[0].extract_text() or '').strip() if reader.pages else ''
    normalized = lambda value: re.sub(r'\s+', '', value).casefold()
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    missing = [line for line in lines if '\ufffd' not in line and
               len(normalized(line)) >= 3 and normalized(line) not in normalized(markdown)]
    audit = {'pdf_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
             'pdf_page': 1, 'method': 'pypdf-native-text',
             'native_text_sha256': hashlib.sha256(raw.encode()).hexdigest(),
             'status': 'supplemented' if missing else ('covered' if raw else 'needs_ocr'),
             'encoding_warning': '\ufffd' in raw, 'missing_lines': missing}
    if not missing:
        return markdown, audit
    block = '# PDF封面原生文本补充（原PDF第1页）\n\n' + raw
    return block + '\n\n' + markdown, audit
