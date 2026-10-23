"""日本語タスク定義と自動採点。

各タスクは dict:
  id, kind, prompt, max_tokens, grader(text, meta) -> (passed: bool, detail: str)

採点方針: PASS/FAIL の二値。細かい段階評価はしない。
"""
import json
import os
import re
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------- helpers


import textwrap


def _strip_fences(text: str) -> str:
    """最初の ```lang ... ``` ブロックの中身を返す。無ければ全体。"""
    m = re.search(r"```[a-zA-Z0-9_+-]*[ \t]*\n(.*?)```", text, re.S)
    return m.group(1) if m else text


def _extract_python(text: str) -> str:
    blocks = re.findall(r"```(?:python|py)?[ \t]*\n(.*?)```", text, re.S)
    if blocks:
        code = blocks[-1]
    else:
        idx = min([i for i in (text.find("def "), text.find("class ")) if i >= 0], default=-1)
        code = text[idx:] if idx >= 0 else text
    # 箇条書き内に貼られたコードブロックは行頭に空白が付くので dedent する
    return textwrap.dedent(code).strip("\n")


def _extract_json(text: str) -> dict | None:
    body = _strip_fences(text).strip()
    # 最初の { から最後の } まで
    i, j = body.find("{"), body.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(body[i:j + 1])
    except Exception:
        return None


def _nums(text: str) -> list[float]:
    """数値を拾う。'1,234' '１２３４' '159.6万' '12億4800万' に対応。"""
    t = text.translate(str.maketrans("０１２３４５６７８９．，", "0123456789.,"))
    t = t.replace(",", "")
    out = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(億|万)?", t):
        v = float(m.group(1))
        if m.group(2) == "万":
            v *= 1e4
        elif m.group(2) == "億":
            v *= 1e8
        out.append(v)
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s))


def _run_code(code: str, tests: str, timeout: int = 30) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sol.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code + "\n\n" + tests)
        try:
            r = subprocess.run([sys.executable, path], capture_output=True,
                               text=True, timeout=timeout, cwd=d)
        except subprocess.TimeoutExpired:
            return False, "timeout"
        if r.returncode == 0 and "ALL_PASS" in r.stdout:
            return True, "ok"
        err = (r.stderr or r.stdout).strip().splitlines()
        return False, " | ".join(err[-3:])[:300]


# ---------------------------------------------------------------- tasks

# --- 1. コード: 単語数カウント
T_CODE_WORDS = {
    "id": "code_count_words",
    "kind": "code",
    "max_tokens": 8192,
    "prompt": """次の仕様を満たす Python 関数 `count_words(text: str) -> int` を実装してください。

仕様:
- 半角スペース・全角スペース・タブ・改行で区切られた単語の数を返す
- 連続する空白は1つの区切りとして扱う
- 前後の空白は無視する
- 空文字列または空白のみの場合は 0 を返す
- 日本語も英語も同じ扱いでよい

出力は Python のコードブロックのみにしてください。説明文は不要です。""",
    "tests": """
assert count_words("hello world") == 2
assert count_words("  a   b\\tc\\n") == 3
assert count_words("") == 0
assert count_words("   ") == 0
assert count_words("日本語 の 文章") == 3
print("ALL_PASS")
""",
}


def grade_code_words(text, meta):
    return _run_code(_extract_python(text), T_CODE_WORDS["tests"])


# --- 2. コード: 金額パース
T_CODE_YEN = {
    "id": "code_yen_to_int",
    "kind": "code",
    "max_tokens": 8192,
    "prompt": """次の仕様を満たす Python 関数 `yen_to_int(s: str) -> int` を実装してください。

仕様:
- "1,234円" / "¥1,000" / "１２３４円" / "500" のような文字列から金額を整数で返す
- 全角数字は半角として扱う
- カンマ・「円」・「¥」・半角/全角スペースは除去する
- 数字が1つも含まれない場合は ValueError を送出する

出力は Python のコードブロックのみにしてください。""",
    "tests": """
assert yen_to_int("1,234円") == 1234
assert yen_to_int("¥1,000") == 1000
assert yen_to_int("１２３４円") == 1234
assert yen_to_int("500") == 500
assert yen_to_int(" 2,000 円 ") == 2000
try:
    yen_to_int("円")
    raise AssertionError("ValueError not raised")
except ValueError:
    pass
print("ALL_PASS")
""",
}


def grade_yen(text, meta):
    return _run_code(_extract_python(text), T_CODE_YEN["tests"])


# --- 3. コード: LRU キャッシュ
T_CODE_LRU = {
    "id": "code_lru_cache",
    "kind": "code",
    "max_tokens": 8192,
    "prompt": """次の仕様を満たす Python クラス `LRUCache` を実装してください。

仕様:
- `LRUCache(capacity: int)` で容量を指定する
- `get(key: int) -> int`: 値を返す。存在しなければ -1
- `put(key: int, value: int) -> None`: 追加/更新。容量超過時は最も長く使われていないキーを追い出す
- get と put はそのキーを「最近使った」扱いにする

出力は Python のコードブロックのみにしてください。""",
    "tests": """
c = LRUCache(2)
c.put(1, 1)
c.put(2, 2)
assert c.get(1) == 1
c.put(3, 3)
assert c.get(2) == -1
assert c.get(3) == 3
assert c.get(1) == 1
c.put(4, 4)
assert c.get(3) == -1
c2 = LRUCache(1)
assert c2.get(9) == -1
c2.put(9, 90)
assert c2.get(9) == 90
c2.put(10, 100)
assert c2.get(9) == -1
print("ALL_PASS")
""",
}


def grade_lru(text, meta):
    return _run_code(_extract_python(text), T_CODE_LRU["tests"])


# --- 4. 情報抽出: 社員情報
T_EXTRACT_EMP = {
    "id": "extract_employee_json",
    "kind": "json",
    "max_tokens": 4096,
    "prompt": """次の文章から情報を抽出し、JSON のみを出力してください。

文章:
「佐藤 健一さんは2018年に当社へ入社し、現在は生産管理部に所属しています。昨年度の年収は620万円でした。趣味は登山です。」

出力スキーマ:
{"氏名": "文字列", "部署": "文字列", "入社年": 整数, "年収": 円単位の整数}

JSON 以外は何も書かないでください。""",
    "expected": {"氏名": "佐藤健一", "部署": "生産管理部", "入社年": 2018, "年収": 6200000},
}


def grade_extract_emp(text, meta):
    d = _extract_json(text)
    if d is None:
        return False, "invalid json"
    exp = T_EXTRACT_EMP["expected"]
    bad = []
    if _norm(d.get("氏名", "")) != exp["氏名"]:
        bad.append(f"氏名={d.get('氏名')!r}")
    if _norm(d.get("部署", "")) != exp["部署"]:
        bad.append(f"部署={d.get('部署')!r}")
    if str(d.get("入社年")) != "2018":
        bad.append(f"入社年={d.get('入社年')!r}")
    try:
        if abs(float(d.get("年収")) - 6200000) > 1:
            bad.append(f"年収={d.get('年収')!r}")
    except Exception:
        bad.append(f"年収={d.get('年収')!r}")
    return (not bad), ("ok" if not bad else "; ".join(bad))


# --- 5. 数値推論（複数ステップ）
T_NUMERIC = {
    "id": "numeric_multi_step",
    "kind": "numeric",
    "max_tokens": 8192,
    "prompt": """次の問題を解いてください。

Aさんは毎月の手取り28万円のうち35%を貯金している。年間ボーナスは手取りの2.5か月分あり、その60%を貯金している。Aさんの1年間の貯金総額はいくらか。

計算過程を書いたあと、最後の行に「答え: <円単位の整数>」の形式で答えを書いてください。""",
    "expected": 1596000,
}


def grade_numeric(text, meta):
    exp = T_NUMERIC["expected"]
    m = re.search(r"答え[:：]\s*([0-9,０-９.,]+)\s*(万|億)?", text)
    cands = []
    if m:
        v = float(m.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789")).replace(",", ""))
        if m.group(2) == "万":
            v *= 1e4
        elif m.group(2) == "億":
            v *= 1e8
        cands.append(v)
    cands += _nums(text)
    for v in cands:
        if abs(v - exp) <= 1:
            return True, "ok"
    return False, f"no match (expected {exp}); tail={text.strip().splitlines()[-1][:80]!r}"


# --- 6. 制約付き要約
T_SUMMARY = {
    "id": "summary_constraint",
    "kind": "text",
    "max_tokens": 4096,
    "prompt": """次の文章を60文字以内で要約してください。必ず「クラウド」と「コスト」という語を含めてください。前置きや説明は書かず、要約本文だけを出力してください。

文章:
「当社では2019年から基幹システムのクラウド移行を進めてきた。当初は移行費用がかさむ懸念があったが、3年間の運用でサーバー維持費と障害対応の人件費が合計で約40%削減された。一方で、クラウド利用料はアクセス増加に比例して増えるため、コスト管理の仕組みづくりが今後の課題となっている。」""",
}


def grade_summary(text, meta):
    body = text.strip().replace("\n", "")
    # 見出しっぽい前置きを除去
    body = re.sub(r"^(要約[:：]|以下は要約です[。:]?)", "", body).strip()
    problems = []
    if "クラウド" not in body:
        problems.append("クラウド無し")
    if "コスト" not in body:
        problems.append("コスト無し")
    if len(body) > 60:
        problems.append(f"{len(body)}文字>60")
    if len(body) < 10:
        problems.append(f"短すぎ{len(body)}")
    return (not problems), ("ok" if not problems else f"{'; '.join(problems)} :: {body[:70]}")


# --- 7. 感情分類（ラベル一致）
T_SENTIMENT = {
    "id": "classify_sentiment",
    "kind": "json",
    "max_tokens": 4096,
    "prompt": """次のレビューの感情を分類し、JSON のみを出力してください。

レビュー:
「配送は遅かったですが、商品の品質は期待以上でした。総合的には満足しています。」

出力スキーマ:
{"sentiment": "positive" または "negative" または "neutral", "score": 1から5の整数}

score は 5 が最も肯定的です。JSON 以外は書かないでください。""",
    "expected": {"sentiment": "positive", "score_min": 4},
}


def grade_sentiment(text, meta):
    d = _extract_json(text)
    if d is None:
        return False, "invalid json"
    s = str(d.get("sentiment", "")).strip().lower()
    try:
        score = int(d.get("score"))
    except Exception:
        return False, f"score invalid: {d.get('score')!r}"
    if s != "positive":
        return False, f"sentiment={s!r}"
    if score < 4:
        return False, f"score={score}"
    return True, "ok"


# --- 8. 長文ニードル抽出
_FILLER = [
    "当社は1978年に大阪で創業し、当初は金属部品の加工を主力事業としていた。",
    "1990年代には海外取引先との連携を強め、東南アジアに現地法人を設立した。",
    "2005年には品質管理部門を独立させ、全社的な検査体制を再構築した。",
    "2011年以降は環境負荷低減を経営課題と位置づけ、設備更新を段階的に進めた。",
    "2016年からはIoTセンサーを導入し、生産ラインの稼働データを可視化している。",
    "2019年に本社機能の一部を東京へ移し、営業と開発の連携を密にした。",
    "2021年には新工場の建設に着手し、省エネ設備と自動搬送システムを導入した。",
    "2022年からは人材育成プログラムを拡充し、若手技術者の海外研修を始めた。",
    "2023年には取引先との共同開発プロジェクトを3件立ち上げた。",
    "2024年は原材料価格の高騰への対応として、調達先の分散を進めた。",
]
_NEEDLE = "2025年度の売上高は12億4800万円、従業員数は342名、国内の拠点数は17か所である。"


def _build_long_doc() -> str:
    parts = []
    for i in range(9):
        for s in _FILLER:
            parts.append(s)
        if i == 5:
            parts.append(_NEEDLE)
    return "\n".join(parts)


T_LONGCTX = {
    "id": "long_context_needle",
    "kind": "json",
    "max_tokens": 4096,
    "prompt": "次の文章を読み、2025年度の業績に関する3つの数値を JSON で出力してください。\n"
              "スキーマ: {\"売上高\": 円単位の整数, \"従業員数\": 整数, \"拠点数\": 整数}\n"
              "JSON 以外は書かないでください。\n\n文章:\n" + _build_long_doc(),
    "expected": {"売上高": 1248000000, "従業員数": 342, "拠点数": 17},
}


def grade_longctx(text, meta):
    d = _extract_json(text)
    if d is None:
        return False, "invalid json"
    exp = T_LONGCTX["expected"]
    bad = []
    for k, v in exp.items():
        try:
            got = float(str(d.get(k, "")).translate(str.maketrans("０１２３４５６７８９", "0123456789")).replace(",", "").replace("円", ""))
        except Exception:
            bad.append(f"{k}={d.get(k)!r}")
            continue
        if abs(got - v) > 1:
            bad.append(f"{k}={got}")
    return (not bad), ("ok" if not bad else "; ".join(bad))


# --- 9. コード: CSV パーサ（クォート・エスケープ・改行）
T_CODE_CSV = {
    "id": "code_csv_parser",
    "kind": "code",
    "max_tokens": 8192,
    "prompt": """次の仕様を満たす Python 関数 `parse_csv(text: str) -> list[list[str]]` を実装してください。

仕様:
- カンマ `,` でフィールドを区切り、改行 `\\n` で行を区切る
- フィールドはダブルクォート `"` で囲むことができる。囲んだ中ではカンマも改行もそのまま含められる
- クォート内の `""` は1つの `"` を表す
- 末尾の改行は無視する（空行を余計に作らない）
- 空文字列の入力は空リスト `[]` を返す
- 連続する改行は空フィールド1個の行として扱う（例: "a\\n\\nb" -> [["a"], [""], ["b"]]）
- 前後の空白はそのまま保持する（トリムしない）

出力は Python のコードブロックのみにしてください。""",
    "tests": """
assert parse_csv("a,b,c") == [["a", "b", "c"]]
assert parse_csv('a,"b,c",d') == [["a", "b,c", "d"]]
assert parse_csv('"a""b",c') == [['a"b', "c"]]
assert parse_csv('a,"line1\\nline2",c') == [["a", "line1\\nline2", "c"]]
assert parse_csv("") == []
assert parse_csv("a,b\\n") == [["a", "b"]]
assert parse_csv("a\\n\\nb") == [["a"], [""], ["b"]]
assert parse_csv(" x , y ") == [[" x ", " y "]]
print("ALL_PASS")
""",
}


def grade_csv(text, meta):
    return _run_code(_extract_python(text), T_CODE_CSV["tests"])


# --- 10. コード: バグ修正（区間マージ）
T_CODE_BUGFIX = {
    "id": "code_bugfix_merge_intervals",
    "kind": "code",
    "max_tokens": 8192,
    "prompt": """次のコードにはバグがあります。仕様を満たすように修正した `merge_intervals` を出力してください。

仕様:
- 閉区間 `[start, end]` のリストを受け取り、重なる区間をマージしたリストを返す
- 接している区間（前の end と次の start が等しい）もマージする
- 入力のリストは変更してはいけない
- 入力が空なら `[]` を返す
- 入力はソートされていなくてもよい

バグのあるコード:
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

出力は Python のコードブロックのみにしてください。""",
    "tests": """
def norm(x):
    return [list(i) for i in x]
assert norm(merge_intervals([[1, 3], [2, 6], [8, 10], [15, 18]])) == [[1, 6], [8, 10], [15, 18]]
assert norm(merge_intervals([[1, 4], [4, 5]])) == [[1, 5]]
assert norm(merge_intervals([[5, 6], [1, 2]])) == [[1, 2], [5, 6]]
assert norm(merge_intervals([])) == []
src = [[1, 3]]
merge_intervals(src)
assert src == [[1, 3]], "入力リストが変更されている"
assert norm(merge_intervals([[1, 10], [2, 3]])) == [[1, 10]]
print("ALL_PASS")
""",
}


def grade_bugfix(text, meta):
    return _run_code(_extract_python(text), T_CODE_BUGFIX["tests"])


# --- 11. コード: 漢数字→整数
T_CODE_KANJI = {
    "id": "code_kanji_number",
    "kind": "code",
    "max_tokens": 8192,
    "prompt": """次の仕様を満たす Python 関数 `kanji_to_int(s: str) -> int` を実装してください。

仕様:
- 漢数字表記の文字列を整数に変換する（例: "千二百三十四" -> 1234）
- 使用する文字: 零 〇 一 二 三 四 五 六 七 八 九 十 百 千 万 億
- 位取り記法に対応する（"三万五千" -> 35000、"一億二千万" -> 120000000）
- "十" 単体は 10、"十五" は 15 のように、先頭の一は省略されうる
- "零" または "〇" は 0
- 想定外の文字が含まれる場合は ValueError を送出する

出力は Python のコードブロックのみにしてください。""",
    "tests": """
assert kanji_to_int("千二百三十四") == 1234
assert kanji_to_int("三万五千") == 35000
assert kanji_to_int("一億二千万") == 120000000
assert kanji_to_int("十") == 10
assert kanji_to_int("十五") == 15
assert kanji_to_int("二百") == 200
assert kanji_to_int("零") == 0
assert kanji_to_int("一億二千三百四十五万六千七百八十九") == 123456789
try:
    kanji_to_int("abc")
    raise AssertionError("ValueError not raised")
except ValueError:
    pass
print("ALL_PASS")
""",
}


def grade_kanji(text, meta):
    return _run_code(_extract_python(text), T_CODE_KANJI["tests"])


# --- 12. 複数文書の突合
T_RECONCILE = {
    "id": "reconcile_invoices",
    "kind": "json",
    "max_tokens": 4096,
    "prompt": """次の2つの文書を突き合わせて、JSON のみを出力してください。

【請求書 2026-09】
- 品目A: 120,000円（税抜）
- 品目B: 80,000円（税抜）
- 品目C: 45,000円（税抜）
消費税は10%で、請求総額に加算する。

【入金明細】
- 2026-09-30 振込 200,000円
- 2026-10-15 振込 49,500円
- 2026-10-20 振込 30,000円（取消済みのため集計から除外）

出力スキーマ:
{"請求総額": 円単位の整数, "入金総額": 円単位の整数, "未入金残高": 円単位の整数}

JSON 以外は書かないでください。""",
    "expected": {"請求総額": 269500, "入金総額": 249500, "未入金残高": 20000},
}


def grade_reconcile(text, meta):
    d = _extract_json(text)
    if d is None:
        return False, "invalid json"
    exp = T_RECONCILE["expected"]
    bad = []
    for k, v in exp.items():
        try:
            got = float(str(d.get(k, "")).translate(str.maketrans("０１２３４５６７８９", "0123456789")).replace(",", "").replace("円", ""))
        except Exception:
            bad.append(f"{k}={d.get(k)!r}")
            continue
        if abs(got - v) > 1:
            bad.append(f"{k}={got}")
    return (not bad), ("ok" if not bad else "; ".join(bad))


# --- 13. 表抽出（フィルタ＋ソート）
_TABLE = """| 部署 | 売上(万円) | 前年比 |
|---|---|---|
| 営業部 | 1250 | +8.2% |
| 製造部 | 980 | -3.1% |
| 開発部 | 640 | +15.5% |
| 管理部 | 210 | +1.0% |
| 物流部 | 470 | -0.5% |
| 営業支援部 | 320 | +4.4% |"""

T_TABLE = {
    "id": "table_extract_filter",
    "kind": "json",
    "max_tokens": 4096,
    "prompt": "次の表から、前年比がプラス（0より大きい）の部署だけを抽出し、"
              "売上(万円)の降順で JSON 配列として出力してください。\n"
              "スキーマ: [{\"部署\": 文字列, \"売上\": 整数, \"前年比\": 小数}]\n"
              "前年比は % を除いた数値（例: +8.2% -> 8.2）。JSON 以外は書かないでください。\n\n"
              + _TABLE,
    "expected": [
        {"部署": "営業部", "売上": 1250, "前年比": 8.2},
        {"部署": "開発部", "売上": 640, "前年比": 15.5},
        {"部署": "営業支援部", "売上": 320, "前年比": 4.4},
        {"部署": "管理部", "売上": 210, "前年比": 1.0},
    ],
}


def grade_table(text, meta):
    body = _strip_fences(text).strip()
    i, j = body.find("["), body.rfind("]")
    if i < 0 or j <= i:
        return False, "invalid json array"
    try:
        got = json.loads(body[i:j + 1])
    except Exception as e:  # noqa: BLE001
        return False, f"json error: {e}"
    exp = T_TABLE["expected"]
    if len(got) != len(exp):
        return False, f"件数 {len(got)} != {len(exp)}"
    for g, e in zip(got, exp):
        if _norm(g.get("部署", "")) != e["部署"]:
            return False, f"部署 {g.get('部署')!r} (expected {e['部署']})"
        try:
            if abs(float(g.get("売上")) - e["売上"]) > 1:
                return False, f"売上 {g.get('売上')!r}"
            if abs(float(str(g.get("前年比")).replace("%", "").replace("+", "")) - e["前年比"]) > 0.05:
                return False, f"前年比 {g.get('前年比')!r}"
        except Exception:
            return False, f"数値変換不可 {g}"
    return True, "ok"


# --- 14. 営業日計算
T_BUSINESS_DAYS = {
    "id": "business_days",
    "kind": "numeric",
    "max_tokens": 4096,
    "prompt": """2026年4月1日（水）から2026年4月30日（木）までの営業日数を求めてください。
土曜日・日曜日と、祝日（4月29日 昭和の日）を除いた日数です。

計算過程を書いたあと、最後の行に「答え: <整数>」の形式で出力してください。""",
    "expected": 21,
}


def grade_business_days(text, meta):
    m = re.search(r"答え[:：]\s*(\d+)", text)
    if m and int(m.group(1)) == T_BUSINESS_DAYS["expected"]:
        return True, "ok"
    nums = _nums(text)
    if nums and abs(nums[-1] - T_BUSINESS_DAYS["expected"]) <= 0.5:
        return True, "ok (答え行なし)"
    return False, f"expected 21; tail={text.strip().splitlines()[-1][:80]!r}"


TASKS = [
    dict(T_CODE_WORDS, grader=grade_code_words),
    dict(T_CODE_YEN, grader=grade_yen),
    dict(T_CODE_LRU, grader=grade_lru),
    dict(T_EXTRACT_EMP, grader=grade_extract_emp),
    dict(T_NUMERIC, grader=grade_numeric),
    dict(T_SUMMARY, grader=grade_summary),
    dict(T_SENTIMENT, grader=grade_sentiment),
    dict(T_LONGCTX, grader=grade_longctx),
    dict(T_CODE_CSV, grader=grade_csv),
    dict(T_CODE_BUGFIX, grader=grade_bugfix),
    dict(T_CODE_KANJI, grader=grade_kanji),
    dict(T_RECONCILE, grader=grade_reconcile),
    dict(T_TABLE, grader=grade_table),
    dict(T_BUSINESS_DAYS, grader=grade_business_days),
]

TASK_BY_ID = {t["id"]: t for t in TASKS}
