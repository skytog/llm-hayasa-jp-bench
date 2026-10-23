"""日本語タスク定義と自動採点。

各タスクは dict:
  id, kind, prompt, max_tokens, grader(text, meta) -> (passed: bool, detail: str)

採点方針: PASS/FAIL の二値。細かい段階評価はしない。
"""
import json
import math
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
        # Python の json は既定で NaN / Infinity を受理するため明示的に拒否する
        def _reject(x):
            raise ValueError(f"non-finite constant: {x}")

        def _no_dup(pairs):
            seen, out = set(), {}
            for k, v in pairs:
                if k in seen:
                    raise ValueError(f"duplicate key: {k}")
                seen.add(k)
                out[k] = v
            return out

        return json.loads(body[i:j + 1], parse_constant=_reject, object_pairs_hook=_no_dup)
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


_DOCKER_OK = None


def _docker_available() -> bool:
    global _DOCKER_OK
    if _DOCKER_OK is None:
        try:
            r = subprocess.run(["sg", "docker", "-c", "docker info"], capture_output=True, timeout=15)
            _DOCKER_OK = r.returncode == 0
        except Exception:  # noqa: BLE001
            _DOCKER_OK = False
    return _DOCKER_OK


def _run_code(code: str, tests: str, timeout: int = 60) -> tuple[bool, str]:
    """候補コードを隔離環境で実行する（specs/scoring.md §2.1）。

    - docker: network none / read-only root / tmpfs / memory 512m / pids 128 / cpus 1
    - sentinel 偽装対策: ハーネスが nonce を生成し、テスト完了後に `ALL_PASS:<nonce>` を出す
    - docker が使えない場合は subprocess にフォールバックし、detail に sandbox=none と記す
    """
    import shlex
    import uuid

    nonce = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        sol = os.path.join(d, "sol.py")
        runner = os.path.join(d, "run_tests.py")
        with open(sol, "w", encoding="utf-8") as f:
            f.write(code + "\n")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(
                "import sys\n"
                "sys.path.insert(0, '.')\n"
                "import sol\n"
                "from sol import *\n"
                + tests
                + f"\nprint('ALL_PASS:{nonce}')\n"
            )
        marker = f"ALL_PASS:{nonce}"

        if _docker_available():
            os.chmod(d, 0o777)
            name = f"llmbench-{nonce[:8]}"
            cmd = [
                "docker", "run", "--rm", "--name", name,
                "--network", "none", "--read-only",
                "--tmpfs", "/tmp:rw,size=8m",
                "-v", f"{d}:/work:rw",
                "--memory", "512m", "--memory-swap", "512m",
                "--pids-limit", "128", "--cpus", "1",
                "-w", "/work",
                "python:3.12-slim", "python", "-I", "-B", "/work/run_tests.py",
            ]
            try:
                r = subprocess.run(["sg", "docker", "-c", shlex.join(cmd)],
                                   capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                subprocess.run(["sg", "docker", "-c", f"docker rm -f {name}"],
                               capture_output=True, timeout=20)
                return False, "timeout (sandbox=docker)"
            if r.returncode == 0 and marker in r.stdout:
                return True, "ok (sandbox=docker)"
            err = (r.stderr or r.stdout).strip().splitlines()
            return False, " | ".join(err[-3:])[:300]

        # フォールバック（隔離なし）
        env = {"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"}
        try:
            r = subprocess.run([sys.executable, "-I", "-B", runner], capture_output=True,
                               text=True, timeout=timeout, cwd=d, env=env)
        except subprocess.TimeoutExpired:
            return False, "timeout (sandbox=none)"
        if r.returncode == 0 and marker in r.stdout:
            return True, "ok (sandbox=none)"
        err = (r.stderr or r.stdout).strip().splitlines()
        return False, " | ".join(err[-3:])[:300]


# ---------------------------------------------------------------- tasks


# ---------------------------------------------------------------------------
# コード採点: 親が期待値を持ち、候補は入力だけを受け取る（nonce 偽装の根治）
# ---------------------------------------------------------------------------


class GraderUnavailable(RuntimeError):
    """隔離実行が使えない（モデルの失敗ではなく測定不能）。"""


ALLOW_NO_SANDBOX = os.environ.get("LLM_BENCH_ALLOW_NO_SANDBOX") == "1"


def _run_cases(code: str, cases: list[dict], timeout: int = 120) -> list[dict]:
    """候補コードを隔離環境で実行し、各ケースの戻り値だけを返す。

    期待値は親プロセスにしか存在しない。候補は cases.json（入力）を読めるが、
    正解を知らないので、正しく計算しない限り一致しない。
    """
    import shlex
    import uuid

    if not _docker_available() and not ALLOW_NO_SANDBOX:
        raise GraderUnavailable("docker unavailable (fail-closed)")

    token = uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        with open(os.path.join(d, "sol.py"), "w", encoding="utf-8") as f:
            f.write(code + "\n")
        with open(os.path.join(d, "cases.json"), "w", encoding="utf-8") as f:
            json.dump(cases, f, ensure_ascii=False)
        with open(os.path.join(d, "runner.py"), "w", encoding="utf-8") as f:
            f.write(
                "import json, sys\n"
                "sys.path.insert(0, '/work')\n"
                "import sol\n"
                "cases = json.load(open('/work/cases.json'))\n"
                "out = []\n"
                "for c in cases:\n"
                "    try:\n"
                "        if 'class' in c:\n"
                "            obj = getattr(sol, c['class'])(*c.get('init', []))\n"
                "            res = []\n"
                "            for op in c['ops']:\n"
                "                res.append(getattr(obj, op[0])(*op[1:]))\n"
                "            out.append({'ok': True, 'result': res})\n"
                "        else:\n"
                "            fn = getattr(sol, c['func'])\n"
                "            args = c.get('args', [])\n"
                "            res = fn(*args)\n"
                "            rec = {'ok': True, 'result': res}\n"
                "            if c.get('echo_args'):\n"
                "                rec['args_after'] = args\n"
                "            out.append(rec)\n"
                "    except Exception as e:\n"
                "        out.append({'ok': False, 'error': type(e).__name__ + ': ' + str(e)[:200]})\n"
                "print('RESULT_JSON:' + json.dumps(out, ensure_ascii=False, default=str))\n"
            )
        os.chmod(d, 0o777)
        if _docker_available():
            name = f"llmbench-{token}"
            cmd = [
                "docker", "run", "--rm", "--name", name,
                "--network", "none", "--read-only",
                "--tmpfs", "/tmp:rw,size=8m",
                "-v", f"{d}:/work:rw",
                "--memory", "512m", "--memory-swap", "512m",
                "--pids-limit", "128", "--cpus", "1",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "-w", "/work",
                "python:3.12-slim", "python", "-I", "-B", "/work/runner.py",
            ]
            try:
                r = subprocess.run(["sg", "docker", "-c", shlex.join(cmd)],
                                   capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                subprocess.run(["sg", "docker", "-c", f"docker rm -f {name}"],
                               capture_output=True, timeout=20)
                return [{"ok": False, "error": "timeout"} for _ in cases]
        else:
            env = {"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"}
            try:
                r = subprocess.run([sys.executable, "-I", "-B", os.path.join(d, "runner.py")],
                                   capture_output=True, text=True, timeout=timeout, cwd=d, env=env)
            except subprocess.TimeoutExpired:
                return [{"ok": False, "error": "timeout"} for _ in cases]

        line = None
        for ln in reversed(r.stdout.splitlines()):
            if ln.startswith("RESULT_JSON:"):
                line = ln[len("RESULT_JSON:"):]
                break
        if line is None:
            err = (r.stderr or "")
            infra = ("docker:", "permission denied", "cannot connect", "no such image",
                     "unable to find image", "pull access denied")
            if any(m in err.lower() for m in infra):
                raise GraderUnavailable(f"container failure: {err.strip()[:300]}")
            # 候補が RESULT_JSON を出さずに終了（os._exit 等）→ 候補の失敗
            return [{"ok": False, "error": "no RESULT_JSON (candidate exited early)"}
                    for _ in cases]
        try:
            parsed = json.loads(line)
        except Exception as e:  # noqa: BLE001
            raise GraderUnavailable(f"runner output unparsable: {e}") from e
        if len(parsed) != len(cases):
            raise GraderUnavailable(f"result count mismatch: {len(parsed)} != {len(cases)}")
        return parsed


def _check_cases(code: str, cases: list[dict], expected: list[dict]) -> tuple[bool, str]:
    results = _run_cases(code, cases)
    for c, res, exp in zip(cases, results, expected):
        if "error" in exp:
            if res.get("ok") or res.get("error", "").split(":")[0] != exp["error"]:
                return False, f"case={c}: expected {exp['error']}, got {res}"
            continue
        if not res.get("ok"):
            return False, f"case={c}: raised {res.get('error')}"
        if res.get("result") != exp["value"]:
            return False, f"case={c}: got {res.get('result')!r}, expected {exp['value']!r}"
        if "args_after" in exp and res.get("args_after") != exp["args_after"]:
            return False, f"case={c}: 入力が変更された {res.get('args_after')!r}"
    return True, f"ok ({len(cases)} cases)"


# --- 参照実装（親側の期待値計算） ---------------------------------------

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_DIGIT = "零一二三四五六七八九"


def _ref_count_words(text: str) -> int:
    return len(text.split())


def _ref_yen(s: str) -> int:
    t = str(s).translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    for ch in (",", "¥", "円", " ", "\u3000"):
        t = t.replace(ch, "")
    if not any(ch.isdigit() for ch in t):
        raise ValueError("no digits")
    return int(t)


def _ref_lru(capacity: int, ops: list) -> list:
    from collections import OrderedDict
    d = OrderedDict()
    out = []
    for op in ops:
        if op[0] == "put":
            k, v = op[1], op[2]
            d[k] = v
            d.move_to_end(k)
            if len(d) > capacity:
                d.popitem(last=False)
            out.append(None)
        else:
            k = op[1]
            if k in d:
                d.move_to_end(k)
                out.append(d[k])
            else:
                out.append(-1)
    return out


def _ref_csv(text: str) -> list:
    if text == "":
        return []
    rows, row, field = [], [], []
    in_q = False
    i, n = 0, len(text)
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
        if ch == "\n":
            row.append("".join(field))
            field = []
            rows.append(row)
            row = []
            i += 1
            continue
        field.append(ch)
        i += 1
    row.append("".join(field))
    rows.append(row)
    if text.endswith("\n") and rows and rows[-1] == [""]:
        rows.pop()
    return rows


def _ref_merge(intervals: list) -> list:
    if not intervals:
        return []
    s = sorted([list(x) for x in intervals])
    out = []
    for a, b in s:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _ref_kanji(s: str) -> int:
    if not s:
        raise ValueError("empty")
    total = section = number = 0
    for ch in s:
        if ch in _DIGITS:
            number = _DIGITS[ch]
        elif ch == "十":
            section += (number or 1) * 10
            number = 0
        elif ch == "百":
            section += (number or 1) * 100
            number = 0
        elif ch == "千":
            section += (number or 1) * 1000
            number = 0
        elif ch == "万":
            total += (section + number) * 10000
            section = number = 0
        elif ch == "億":
            total += (section + number) * 100000000
            section = number = 0
        else:
            raise ValueError(f"unexpected char: {ch}")
    return total + section + number


def _kanji_format(n: int) -> str:
    if n == 0:
        return "零"
    out = ""
    for val, unit in ((10 ** 8, "億"), (10 ** 4, "万")):
        d, n = divmod(n, val)
        if d:
            out += _kanji_format(d) + unit
    for val, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        d, n = divmod(n, val)
        if d:
            out += ("" if (d == 1 and unit) else _KANJI_DIGIT[d]) + unit
    return out


def _code_cases(kind: str, rng) -> tuple[list, list]:
    """ランダムなケースと親側の期待値を生成する。"""
    cases, expected = [], []
    if kind == "count_words":
        pieces = ["a", "b", "日本語", "x1", "カタカナ"]
        seps = [" ", "  ", "\t", "\n", "\u3000", " \t "]
        for _ in range(8):
            k = rng.randint(1, 5)
            body = "".join(rng.choice(pieces) + (rng.choice(seps) if i < k - 1 else "")
                           for i in range(k))
            body = rng.choice(["", " "]) + body + rng.choice(["", "\u3000", "\n"])
            cases.append({"func": "count_words", "args": [body]})
            expected.append({"value": _ref_count_words(body)})
        for body in ["", "   ", "\u3000"]:
            cases.append({"func": "count_words", "args": [body]})
            expected.append({"value": _ref_count_words(body)})
    elif kind == "yen":
        for _ in range(8):
            v = rng.randint(0, 9_999_999)
            style = rng.choice(["comma", "yen", "zen", "plain", "space"])
            if style == "comma":
                s = f"{v:,}円"
            elif style == "yen":
                s = f"¥{v:,}"
            elif style == "zen":
                s = str(v).translate(str.maketrans("0123456789", "０１２３４５６７８９")) + "円"
            elif style == "space":
                s = f" {v:,} 円 "
            else:
                s = str(v)
            cases.append({"func": "yen_to_int", "args": [s]})
            expected.append({"value": _ref_yen(s)})
        for bad in ["円", "¥", ""]:
            cases.append({"func": "yen_to_int", "args": [bad]})
            expected.append({"error": "ValueError"})
    elif kind == "lru":
        for _ in range(4):
            cap = rng.randint(1, 3)
            ops = []
            for _ in range(rng.randint(8, 14)):
                k = rng.randint(1, 5)
                if rng.random() < 0.5:
                    ops.append(["put", k, rng.randint(1, 99)])
                else:
                    ops.append(["get", k])
            cases.append({"class": "LRUCache", "init": [cap], "ops": ops})
            expected.append({"value": _ref_lru(cap, ops)})
    elif kind == "csv":
        pool = ['a', 'b', 'c', 'x,y', 'q"z', 'l1\nl2', ' sp ', '']
        for _ in range(6):
            rows = []
            for _ in range(rng.randint(1, 3)):
                rows.append([rng.choice(pool) for _ in range(rng.randint(1, 4))])
            lines = []
            for row in rows:
                fields = []
                for f in row:
                    if any(ch in f for ch in [',', '"', '\n']):
                        fields.append('"' + f.replace('"', '""') + '"')
                    else:
                        fields.append(f)
                lines.append(",".join(fields))
            text = "\n".join(lines) + rng.choice(["", "\n"])
            cases.append({"func": "parse_csv", "args": [text]})
            expected.append({"value": _ref_csv(text)})
        for text in ["", "a\n\nb", " x , y "]:
            cases.append({"func": "parse_csv", "args": [text]})
            expected.append({"value": _ref_csv(text)})
    elif kind == "merge":
        for _ in range(6):
            iv = []
            for _ in range(rng.randint(0, 5)):
                a = rng.randint(0, 20)
                iv.append([a, a + rng.randint(0, 5)])
            rng.shuffle(iv)
            cases.append({"func": "merge_intervals", "args": [iv], "echo_args": True})
            expected.append({"value": _ref_merge(iv), "args_after": [iv]})
    elif kind == "kanji":
        for _ in range(8):
            n = rng.choice([rng.randint(0, 9), rng.randint(10, 99),
                             rng.randint(100, 999), rng.randint(1000, 9999),
                             rng.randint(10 ** 4, 10 ** 6), rng.randint(10 ** 6, 10 ** 8)])
            s = _kanji_format(n)
            cases.append({"func": "kanji_to_int", "args": [s]})
            expected.append({"value": n})
        for bad in ["abc", "十a"]:
            cases.append({"func": "kanji_to_int", "args": [bad]})
            expected.append({"error": "ValueError"})
    return cases, expected


def _grade_code_kind(text: str, kind: str, seed: int = 1234) -> tuple[bool, str]:
    import random
    code = _extract_python(text)
    if not code.strip():
        return False, "コードブロックが無い"
    cases, expected = _code_cases(kind, random.Random(seed))
    return _check_cases(code, cases, expected)


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
    return _grade_code_kind(text, "count_words")


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
    return _grade_code_kind(text, "yen")


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
    return _grade_code_kind(text, "lru")


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
    if set(d.keys()) != {"氏名", "部署", "入社年", "年収"}:
        bad.append(f"キー集合={sorted(d.keys())}")
    if _norm(d.get("氏名", "")) != exp["氏名"]:
        bad.append(f"氏名={d.get('氏名')!r}")
    if _norm(d.get("部署", "")) != exp["部署"]:
        bad.append(f"部署={d.get('部署')!r}")
    if not (isinstance(d.get("入社年"), int) and not isinstance(d.get("入社年"), bool)
            and d["入社年"] == exp["入社年"]):
        bad.append(f"入社年={d.get('入社年')!r}")
    v = d.get("年収")
    if not (isinstance(v, int) and not isinstance(v, bool) and v == exp["年収"]):
        bad.append(f"年収={v!r} (整数が必要)")
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
    """最終行の `答え: <整数>` のみを見る（specs/scoring.md §2.3）。"""
    exp = T_NUMERIC["expected"]
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return False, "empty"
    m = re.match(r"答え[:：]\s*([0-9０-９,]+)\s*(万|億)?\s*$", lines[-1])
    if not m:
        return False, f"最終行に答えが無い: {lines[-1][:80]!r}"
    raw = m.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if "," in raw and not re.fullmatch(r"[0-9]{1,3}(,[0-9]{3})+", raw):
        return False, f"不正な桁区切り: {raw}"
    v = int(raw.replace(",", ""))
    if m.group(2) == "万":
        v *= 10000
    elif m.group(2) == "億":
        v *= 100000000
    return (v == exp), (f"got {v} (expected {exp})" if v != exp else "ok")


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
    """字数とキーワードのみを見る = 制約通過率（意味品質ではない）。"""
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
    score = d.get("score")
    if s != "positive":
        return False, f"sentiment={s!r}"
    if not (isinstance(score, int) and not isinstance(score, bool) and 1 <= score <= 5):
        return False, f"score={score!r} (1..5の整数が必要)"
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
        got = d.get(k)
        if not (isinstance(got, int) and not isinstance(got, bool) and got == v):
            bad.append(f"{k}={got!r}")
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
    return _grade_code_kind(text, "csv")


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
src = [[8, 10], [1, 3], [2, 6]]
merge_intervals(src)
assert src == [[8, 10], [1, 3], [2, 6]], "入力リストが変更されている"
assert norm(merge_intervals([[1, 10], [2, 3]])) == [[1, 10]]
print("ALL_PASS")
""",
}


def grade_bugfix(text, meta):
    return _grade_code_kind(text, "merge")


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
    return _grade_code_kind(text, "kanji")


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
        got = d.get(k)
        if not (isinstance(got, int) and not isinstance(got, bool) and got == v):
            bad.append(f"{k}={got!r}")
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
    if not isinstance(got, list):
        return False, f"not a list: {type(got).__name__}"
    exp = T_TABLE["expected"]
    if len(got) != len(exp):
        return False, f"件数 {len(got)} != {len(exp)}"
    for g, e in zip(got, exp):
        if not isinstance(g, dict):
            return False, f"要素がobjectでない: {g!r}"
        if _norm(g.get("部署", "")) != e["部署"]:
            return False, f"部署 {g.get('部署')!r} (expected {e['部署']})"
        sv, pv = g.get("売上"), g.get("前年比")
        if not (isinstance(sv, int) and not isinstance(sv, bool) and sv == e["売上"]):
            return False, f"売上 {sv!r} (expected int {e['売上']})"
        if not (isinstance(pv, (int, float)) and not isinstance(pv, bool) and math.isfinite(pv)
                and abs(float(pv) - e["前年比"]) <= 0.05):
            return False, f"前年比 {pv!r}"
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
    """最終行の `答え: <整数>` のみを見る。"""
    exp = T_BUSINESS_DAYS["expected"]
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return False, "empty"
    m = re.match(r"答え[:：]\s*(\d+)\s*$", lines[-1])
    if not m:
        return False, f"最終行に整数の答えが無い: {lines[-1][:80]!r}"
    got = int(m.group(1))
    return (got == exp), (f"got {got} (expected {exp})" if got != exp else "ok")


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
