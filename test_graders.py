"""採点ロジックの自己テスト（LLMを介さず正解/不正解を流し込む）."""
from tasks import TASK_BY_ID

GOOD = {
    "code_count_words": '''
```python
def count_words(text: str) -> int:
    return len(text.split())
```
''',
    "code_yen_to_int": '''
```python
import re

def yen_to_int(s: str) -> int:
    t = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    t = t.replace(",", "").replace("¥", "").replace("円", "").replace(" ", "").replace("　", "")
    if not re.search(r"\\d", t):
        raise ValueError(s)
    return int(t)
```
''',
    "code_lru_cache": '''
```python
from collections import OrderedDict

class LRUCache:
    def __init__(self, capacity: int):
        self.cap = capacity
        self.d = OrderedDict()

    def get(self, key: int) -> int:
        if key not in self.d:
            return -1
        self.d.move_to_end(key)
        return self.d[key]

    def put(self, key: int, value: int) -> None:
        if key in self.d:
            self.d.move_to_end(key)
        self.d[key] = value
        if len(self.d) > self.cap:
            self.d.popitem(last=False)
```
''',
    "extract_employee_json": '{"氏名": "佐藤 健一", "部署": "生産管理部", "入社年": 2018, "年収": 6200000}',
    "numeric_multi_step": "毎月: 280000*0.35=98000、年間1176000。ボーナス: 700000*0.6=420000。合計1596000。\n答え: 1596000",
    "summary_constraint": "クラウド移行で運用コストを40%削減できたが、利用量に応じたコスト管理が課題。",
    "classify_sentiment": '{"sentiment": "positive", "score": 4}',
    "long_context_needle": '{"売上高": 1248000000, "従業員数": 342, "拠点数": 17}',
    "code_csv_parser": '''
```python
def parse_csv(text):
    if text == "":
        return []
    rows, row, field = [], [], []
    i, n = 0, len(text)
    in_q = False
    while i < n:
        ch = text[i]
        if in_q:
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    field.append('"')
                    i += 2
                    continue
                in_q = False
                i += 1
                continue
            field.append(ch)
            i += 1
            continue
        if ch == '"':
            in_q = True
            i += 1
            continue
        if ch == ",":
            row.append("".join(field))
            field = []
            i += 1
            continue
        if ch == "\\n":
            row.append("".join(field))
            rows.append(row)
            row, field = [], []
            i += 1
            continue
        field.append(ch)
        i += 1
    row.append("".join(field))
    if not (row == [""] and rows and text.endswith("\\n")):
        rows.append(row)
    return rows
```
''',
    "code_bugfix_merge_intervals": '''
```python
def merge_intervals(intervals):
    items = sorted(list(intervals))
    out = []
    for s, e in items:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out
```
''',
    "code_kanji_number": '''
```python
DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
UNITS = {"十": 10, "百": 100, "千": 1000}
BIG = {"万": 10**4, "億": 10**8}

def kanji_to_int(s):
    total = section = num = 0
    seen = False
    for ch in s:
        if ch in DIGITS:
            num = DIGITS[ch]
            seen = True
        elif ch in UNITS:
            if num == 0:
                num = 1
            section += num * UNITS[ch]
            num = 0
            seen = True
        elif ch in BIG:
            section += num
            total += section * BIG[ch]
            section = num = 0
            seen = True
        else:
            raise ValueError(ch)
    if not seen:
        raise ValueError(s)
    return total + section + num
```
''',
    "reconcile_invoices": '{"請求総額": 269500, "入金総額": 249500, "未入金残高": 20000}',
    "table_extract_filter": '[{"部署": "営業部", "売上": 1250, "前年比": 8.2}, {"部署": "開発部", "売上": 640, "前年比": 15.5}, {"部署": "営業支援部", "売上": 320, "前年比": 4.4}, {"部署": "管理部", "売上": 210, "前年比": 1.0}]',
    "business_days": "4月は30日。土日は8日、祝日は1日。30-8-1=21。\n答え: 21",
}

BAD = {
    "code_count_words": "```python\ndef count_words(text):\n    return len(text)\n```",
    "code_yen_to_int": "```python\ndef yen_to_int(s):\n    return int(s)\n```",
    "code_lru_cache": "```python\nclass LRUCache:\n    def __init__(self, c): self.d = {}\n    def get(self, k): return self.d.get(k, -1)\n    def put(self, k, v): self.d[k] = v\n```",
    "extract_employee_json": '{"氏名": "佐藤健一", "部署": "生産管理部", "入社年": "2018年", "年収": "620万円"}',
    "numeric_multi_step": "答え: 1176000",
    "summary_constraint": "クラウド移行によってサーバー維持費と障害対応の人件費が削減されたが、クラウド利用料はアクセス増加に比例して増えるため、コスト管理の仕組みづくりが今後の課題となっている。",
    "classify_sentiment": '{"sentiment": "neutral", "score": 3}',
    "long_context_needle": '{"売上高": 124800000, "従業員数": 342, "拠点数": 17}',
    "code_csv_parser": '''
```python
def parse_csv(text):
    return [line.split(",") for line in text.strip().split("\\n")]
```
''',
    "code_bugfix_merge_intervals": '''
```python
def merge_intervals(intervals):
    intervals.sort()
    out = []
    for s, e in intervals:
        if out and s < out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out
```
''',
    "code_kanji_number": '''
```python
def kanji_to_int(s):
    table = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
    return table.get(s, 0)
```
''',
    "reconcile_invoices": '{"請求総額": 269500, "入金総額": 279500, "未入金残高": -10000}',
    "table_extract_filter": '[{"部署": "営業部", "売上": 1250, "前年比": 8.2}, {"部署": "製造部", "売上": 980, "前年比": -3.1}]',
    "business_days": "答え: 22",
}

ok = True
for tid, task in TASK_BY_ID.items():
    p, d = task["grader"](GOOD[tid], {})
    q, e = task["grader"](BAD[tid], {})
    status = "OK " if (p and not q) else "NG "
    if not (p and not q):
        ok = False
    print(f"{status}{tid:24s} good={p} bad={q}  ({d[:40]} / {e[:40]})")

print("\nALL GOOD" if ok else "\nGRADER BUG")
