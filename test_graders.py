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
