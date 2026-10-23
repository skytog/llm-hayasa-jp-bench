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


TASKS = [
    dict(T_CODE_WORDS, grader=grade_code_words),
    dict(T_CODE_YEN, grader=grade_yen),
    dict(T_CODE_LRU, grader=grade_lru),
    dict(T_EXTRACT_EMP, grader=grade_extract_emp),
    dict(T_NUMERIC, grader=grade_numeric),
    dict(T_SUMMARY, grader=grade_summary),
    dict(T_SENTIMENT, grader=grade_sentiment),
    dict(T_LONGCTX, grader=grade_longctx),
]

TASK_BY_ID = {t["id"]: t for t in TASKS}
