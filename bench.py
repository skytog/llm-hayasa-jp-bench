#!/usr/bin/env python3
"""日本語 LLM ベンチマーク.

測るもの:
  1. Prefill tok/s  -- プロンプト長を変えた TTFT の傾きから算出（ネットワーク往復とキュー待ちを切片として分離）
  2. Decode tok/s   -- ストリーミングのトークン間隔（ITL）から算出
  3. Task Completion Time -- 固定日本語タスクの実測 wall time
  4. Task Success Rate    -- 自動採点の PASS/FAIL
  5. Time-to-Pass         -- 平均Task完了時間 / 成功率（成功1件を得るまでの期待時間）

使い方:
  python3 bench.py --model gemma-4-26B-A4B-it --runs 3
  python3 bench.py --model A,B --tasks code_lru_cache,numeric_multi_step --runs 1
"""
from __future__ import annotations

import argparse
import hashlib
import random
import queue
import threading
import json
import os
import re
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

import requests

from tasks import TASKS, TASK_BY_ID

DEFAULT_BASE = "https://127.0.0.1:8000/v1"
# 認証付きエンドポイント（OpenRouter 等）用。CLI の --api-key でも上書きできる
_API_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("LLM_API_KEY")


# ------------------------------------------------------------------ HTTP


def _sse_events(resp, deadline=None, max_event_bytes=1_000_000, max_total_bytes=64_000_000):
    """SSE のイベント境界（空行）で区切って返す。CRLF も扱う。

    受信バイト単位で絶対期限・イベントサイズ・総受信量を制限する
    （区切りのない長いイベントが buf を無限に伸ばすのを防ぐ）。
    """
    buf = b""
    total = 0
    # iter_content(chunk_size=N) は N バイト貯まるまで返さないことがあり、TTFT を過大にする。
    # read1 は「今ある分」を返すので、最初の delta の到着時刻を素直に観測できる。
    chunks = iter(lambda: resp.raw.read1(65536, decode_content=True), b"")
    for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if total > max_total_bytes:
            raise RuntimeError(f"response too large: {total} bytes")
        if deadline is not None and time.perf_counter() > deadline:
            raise TimeoutError("absolute deadline exceeded (while receiving bytes)")
        buf += chunk
        buf = buf.replace(b"\r\n", b"\n")
        while b"\n\n" in buf:
            event, buf = buf.split(b"\n\n", 1)
            if len(event) > max_event_bytes:
                raise RuntimeError(f"SSE event too large: {len(event)} bytes")
            yield event.decode("utf-8", "replace")
    if buf.strip():
        yield buf.decode("utf-8", "replace")


def _parse_event(event: str):
    lines = [ln[5:].lstrip() for ln in event.splitlines() if ln.startswith("data:")]
    if not lines:
        return None
    payload = "\n".join(lines)
    try:
        return json.loads(payload)
    except Exception:
        try:
            return json.loads("".join(lines))
        except Exception:
            return None


def _repeat_ratio(text: str) -> float | None:
    """reasoning の文字 8-gram 重複率（反復・degeneration の検出）。

    行単位だと「言い換えループ」を取り逃すため、空白除去後の文字 n-gram で測る。
    """
    import re
    from collections import Counter

    t = re.sub(r"\s+", "", text)
    if len(t) < 400:
        return None
    n, stride = 8, 2
    grams = [t[i:i + n] for i in range(0, len(t) - n, stride)]
    if not grams:
        return None
    c = Counter(grams)
    dup = sum(v for v in c.values() if v > 1)
    return round(dup / len(grams), 3)


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """二項比率の Wilson 95% 信頼区間。"""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _is_done(event: str) -> bool:
    """SSE の終端は `data: [DONE]` という行の完全一致で判定する。

    本文中に文字列 "[DONE]" が含まれるだけでは終端とみなさない。
    """
    return any(line.strip() == "data: [DONE]" for line in event.splitlines())


def chat_stream(base, model, prompt, max_tokens, temperature=0.0, timeout=600, retry=0,
                require_done=True):
    """1リクエストをストリーミングで実行し、観測契約（specs/metrics.md §1）に沿った値を返す。

    - 終端: `data: [DONE]` の完全一致（require_done=True なら無い場合 truncated）
    - deadline: monotonic な絶対期限。read timeout は idle 用に短く取る
    - usage 不明は state と分離し、degraded として記録する
    - 失敗しても elapsed（失敗確定までの時間）を必ず返す
    state: ok / empty / truncated / http_error / timeout / error
    """
    url = base.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.perf_counter()
    deadline = t0 + timeout
    ttft_any = None
    ttft_answer = None
    chunk_times = []
    content_times = []
    content_parts = []
    reasoning_parts = []
    usage = {}
    finish_reason = None
    done_seen = False
    parse_errors = 0
    last_err = None
    state = "ok"
    for attempt in range(retry + 1):
        try:
            headers = {"Authorization": f"Bearer {_API_KEY}"} if _API_KEY else {}
            r = requests.post(url, json=body, headers=headers, stream=True,
                              timeout=(10, min(30.0, max(1.0, timeout))))
            try:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                # 読み取りは別スレッドに任せる。終端後や deadline 後は消費をやめるだけでよく、
                # 接続が閉じないサーバーでも Task Time に待ち時間が混ざらない
                q = queue.Queue()

                def _reader(resp):
                    try:
                        for ev in _sse_events(resp, deadline=deadline):
                            q.put(("event", ev))
                    except Exception as e:  # noqa: BLE001
                        q.put(("error", f"{type(e).__name__}: {e}"))
                    finally:
                        q.put(("end", None))
                        try:
                            resp.close()  # ソケット解放（drain はこのスレッド内で行う）
                        except Exception:  # noqa: BLE001
                            pass

                threading.Thread(target=_reader, args=(r,), daemon=True).start()
                while True:
                    try:
                        kind, payload = q.get(timeout=1.0)
                    except queue.Empty:
                        if time.perf_counter() > deadline:
                            state = "timeout"
                            last_err = f"absolute deadline {timeout}s exceeded"
                            break
                        continue
                    if kind == "end":
                        break
                    if kind == "error":
                        if "deadline" in payload.lower():
                            state = "timeout"
                        else:
                            state = "error"
                        last_err = payload
                        break
                    event = payload
                    if time.perf_counter() > deadline:
                        state = "timeout"
                        last_err = f"absolute deadline {timeout}s exceeded"
                        break
                    if _is_done(event):
                        done_seen = True
                        break
                    obj = _parse_event(event)
                    if obj is None:
                        parse_errors += 1
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    for ch in obj.get("choices") or []:
                        if ch.get("finish_reason"):
                            finish_reason = ch["finish_reason"]
                        d = ch.get("delta") or {}
                        piece = d.get("content")
                        # provider によって reasoning のフィールド名が違う
                        # （vLLM/DeepSeek 直: reasoning_content / OpenRouter: reasoning）
                        think = d.get("reasoning_content") or d.get("reasoning")
                        if piece or think:
                            now = time.perf_counter() - t0
                            if ttft_any is None:
                                ttft_any = now
                            if piece and ttft_answer is None:
                                ttft_answer = now
                            chunk_times.append(now)
                            if piece:
                                content_parts.append(piece)
                                content_times.append(now)
                            if think:
                                reasoning_parts.append(think)
                if state == "timeout":
                    break
            finally:
                # close は drain して接続を再利用しようとするので呼ばない（daemon スレッドが保持）
                pass
            break
        except requests.exceptions.Timeout as e:  # noqa: BLE001
            last_err = e
            state = "timeout"
        except Exception as e:  # noqa: BLE001
            last_err = e
            state = "http_error" if "HTTP" in str(e) else "error"
        if attempt < retry:
            time.sleep(1)

    total = time.perf_counter() - t0
    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    text = content
    completion = usage.get("completion_tokens")
    prompt_tokens = usage.get("prompt_tokens")
    usage_available = isinstance(completion, int) and isinstance(prompt_tokens, int)
    itls = [b - a for a, b in zip(chunk_times, chunk_times[1:])]
    span = (chunk_times[-1] - chunk_times[0]) if len(chunk_times) > 1 else None
    # content 区間だけの速度（reasoning の時間を含めない = 純粋な日本語生成の速さ）
    content_span = (content_times[-1] - content_times[0]) if len(content_times) > 1 else None
    # 本文が一気に届く（バースト）と span が極小になり速度が過大になるので、分割数を要求する
    content_cps = (len(content) / content_span) if (
        content_span and content_span > 0.05 and len(content_times) >= 5 and len(content)) else None
    R_out = ((completion - 1) / span) if (span and usage_available and completion > 1) else None

    if state == "ok":
        if require_done and not done_seen:
            state = "truncated"
        elif finish_reason is None:
            state = "truncated"  # 仕様: finish_reason が無い正常終了は truncated
        elif not content.strip():
            state = "empty"

    return {
        "text": text,
        "content": content,
        "reasoning": reasoning,
        "ttft": ttft_any,
        "ttft_any": ttft_any,
        "ttft_answer": ttft_answer,
        "l": chunk_times[-1] if chunk_times else None,
        "chunk_count": len(chunk_times),
        "content_chunk_count": len(content_times),
        "content_span": content_span,
        "content_chars_per_s": round(content_cps, 2) if content_cps else None,
        "total": total,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion,
        "itls": itls,
        "R_out": R_out,
        "usage": usage,
        "usage_available": usage_available,
        "finish_reason": finish_reason,
        "done_seen": done_seen,
        "parse_errors": parse_errors,
        "state": state,
        "degraded": (parse_errors > 0) or (not usage_available),
        "error": str(last_err) if last_err else None,
        "reasoning_chars": len(reasoning),
        "content_chars": len(content),
        "reasoning_repeat_ratio": _repeat_ratio(reasoning),
    }


FILLER = (
    "当社は2024年度からクラウド基盤への移行を進めており、現在は基幹系システムの7割が移行済みです。"
    "移行に伴い運用コストは前年比12%削減され、障害対応時間も平均40分から18分に短縮されました。"
    "一方で、移行に不慣れな現場部門からの問い合わせが増えており、教育コストの増加が課題となっています。"
    "2025年度は残り3割の移行と、現場向けの研修プログラムの整備を同時に進める計画です。"
    "本資料では、移行の進捗、コスト削減効果、現場の課題、次年度の計画について順に説明します。"
)


def build_prompt(chars: int) -> str:
    """指定文字数の日本語プロンプト。先頭に salt を置き、user 本文の cache 共有を防ぐ。

    注意: salt は user 本文の先頭だけなので、system/template prefix の cache hit は防がない。
    """
    head = f"資料番号 {uuid.uuid4().hex}。以下は社内文書です。内容を一文で要約してください。\n\n"
    reps = max(1, chars // len(FILLER) + 1)
    body = (FILLER * reps)[:chars]
    return head + body


def wait_ready(base, model, temperature=0.0, max_wait=1800):
    """モデルが応答可能になるまで待つ（warmup 用）。

    TTFT < 3s は「warm の代理指標」であり、resident 状態の確認ではない。
    最初の観測 TTFT を cold と呼ぶのも、モデル未ロードを確認した場合に限る。
    """
    t_start = time.perf_counter()
    first_ttft = None
    attempts = 0
    while time.perf_counter() - t_start < max_wait:
        attempts += 1
        w = chat_stream(base, model, "こんにちは。短く返して。", max_tokens=8,
                        temperature=temperature, timeout=900)
        t = w["ttft"]
        if first_ttft is None:
            first_ttft = t
        if t is not None and t < 3.0 and w["state"] in ("ok", "empty"):
            return first_ttft, attempts
        time.sleep(10)
    raise RuntimeError(f"{model}: モデルが {max_wait}s 以内に準備完了しなかった")


def _bootstrap_slope_ci(blocks, n_boot=2000, seed=1234):
    """ブロック単位のクラスタ・ブートストラップで傾きの95%CI（tok/s に逆変換）を出す。"""
    rng = random.Random(seed)
    slopes = []
    for _ in range(n_boot):
        pick = [blocks[rng.randrange(len(blocks))] for _ in blocks]
        pairs = [p for b in pick for p in b]
        xs = [x for x, _ in pairs]
        ys = [y for _, y in pairs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        if sxx > 0 and sxy > 0:
            slopes.append(sxy / sxx)
    if not slopes:
        return None
    slopes.sort()
    lo = slopes[int(0.025 * len(slopes))]
    hi = slopes[min(len(slopes) - 1, int(0.975 * len(slopes)))]
    return [round(1 / hi, 1), round(1 / lo, 1)]


def _theil_sen(pairs):
    """全対の傾きの中央値。外れ値に強い（OLS の代替）。"""
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    slopes = []
    for i in range(len(xs)):
        for j in range(i + 1, len(xs)):
            dx = xs[j] - xs[i]
            if dx:
                slopes.append((ys[j] - ys[i]) / dx)
    return statistics.median(slopes) if slopes else None


def _pct(vals, q):
    v = sorted(vals)
    return v[min(len(v) - 1, int(q * len(v)))] if v else None


def measure_prefill(base, model, lengths, temperature=0.0, reps=3, gap=2.0, blocks=3):
    """TTFT_any = a + b * prompt_tokens を全有効対で最小二乗し、1/b とブロックCIを返す。

    - 変数は `TTFT_any`（最初の非空 delta）。`TTFT_answer` は `ttft_answer_valid` として別記録
    - ブロック内で長さ順をランダム化（時間ドリフトと長さ効果を分離する）
    - 遅い試行を捨てない。全滅した長さは `failed_lengths` に残す
    - 傾きの 95%CI は**ブロック単位のクラスタ・ブートストラップ**
    """
    chat_stream(base, model, build_prompt(min(lengths)), max_tokens=8,
                temperature=temperature, timeout=900)
    time.sleep(0.5)
    pairs, block_pairs, attempts_all = [], [], []
    per_len = {c: {"ttft": [], "tok": []} for c in lengths}
    answer_valid = 0
    order_rng = random.Random(4242)
    for b in range(blocks):
        order = list(lengths)
        order_rng.shuffle(order)
        this_block = []
        for chars in order:
            for k in range(reps):
                prompt = build_prompt(chars)  # 反復ごとに salt を作り直す
                r = chat_stream(base, model, prompt, max_tokens=8, temperature=temperature, timeout=600)
                valid = (r["ttft_any"] is not None and r["prompt_tokens"]
                         and r["state"] in ("ok", "empty", "truncated"))
                attempts_all.append({"block": b, "chars": chars, "rep": k,
                                     "ttft_any": r["ttft_any"], "ttft_answer": r["ttft_answer"],
                                     "prompt_tokens": r["prompt_tokens"], "state": r["state"],
                                     "finish_reason": r["finish_reason"], "valid": bool(valid)})
                if valid:
                    pairs.append((r["prompt_tokens"], r["ttft_any"]))
                    this_block.append((r["prompt_tokens"], r["ttft_any"]))
                    per_len[chars]["ttft"].append(r["ttft_any"])
                    per_len[chars]["tok"].append(r["prompt_tokens"])
                    if r["ttft_answer"] is not None:
                        answer_valid += 1
                if k < reps - 1:
                    time.sleep(gap)
            time.sleep(0.2)
        if this_block:
            block_pairs.append(this_block)

    failed_lengths = [c for c in lengths if not per_len[c]["ttft"]]
    samples = []
    for c in lengths:
        tt = per_len[c]["ttft"]
        tk = per_len[c]["tok"]
        samples.append({
            "chars": c,
            "n": len(tt),
            "ttft_median": round(statistics.median(tt), 4) if tt else None,
            "prompt_tokens_median": round(statistics.median(tk), 1) if tk else None,
            "tokens_per_char": round(statistics.median(tk) / c, 3) if tk else None,
        })

    total_req = blocks * reps * len(lengths)
    if len(pairs) < 2:
        return {"prefill_tps": None, "prefill_ci95": None, "intercept_s": None, "r2": None,
                "samples": samples, "mean_tokens_per_char": None,
                "requested": total_req, "valid": len(pairs), "failed": total_req - len(pairs),
                "probe_reps": reps, "blocks": blocks, "failed_lengths": failed_lengths,
                "ttft_answer_valid": answer_valid, "pairs": [],
                "note": "有効対が2未満のため傾きを推定できない"}

    xs = [x for x, _ in pairs]
    ys = [y for _, y in pairs]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx if sxx else None
    a = (my - b * mx) if b is not None else None
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys)) if b is not None else None
    r2 = (1 - ss_res / ss_tot) if (b is not None and ss_tot > 0) else None
    tpc = [s["tokens_per_char"] for s in samples if s["tokens_per_char"]]
    ts = _theil_sen(pairs)
    all_ttft = [y for _, y in pairs]
    ttft_p50 = statistics.median(all_ttft) if all_ttft else None
    ttft_p95 = _pct(all_ttft, 0.95)
    return {
        "prefill_tps": (1 / b) if (b and b > 0) else None,
        "prefill_tps_robust": (1 / ts) if (ts and ts > 0) else None,
        "ttft_p50_s": round(ttft_p50, 4) if ttft_p50 else None,
        "ttft_p95_s": round(ttft_p95, 4) if ttft_p95 else None,
        "ttft_jitter_p95_p50": round(ttft_p95 / ttft_p50, 2) if (ttft_p50 and ttft_p95) else None,
        "prefill_ci95": _bootstrap_slope_ci(block_pairs) if len(block_pairs) >= 2 else None,
        "intercept_s": a,
        "r2": r2,
        "samples": samples,
        "mean_tokens_per_char": round(statistics.mean(tpc), 3) if tpc else None,
        "requested": total_req,
        "valid": len(pairs),
        "failed": total_req - len(pairs),
        "probe_reps": reps,
        "blocks": blocks,
        "failed_lengths": failed_lengths,
        "ttft_answer_valid": answer_valid,
        "pairs": [[x, round(y, 4)] for x, y in pairs],
        "note": ("TTFT_any の全有効対による傾き。3ブロック×ランダム順。CI はブロック・ブートストラップ "
                 "(n=2000)。切片はネットワーク+キュー待ち+固定費の合計で内訳ではない"),
    }


DECODE_PROMPT = "1から200までの整数を1行に1つずつ、何も付け加えずに出力してください。"
DECODE_TOKENS = 256
# 日本語の散文 decode を測るための probe（ASCII の数字列では日本語負荷が見えない）
DECODE_PROSE_PROMPT = (
    "「地方の中小企業が業務システムをクラウドへ移行する際の注意点」について、"
    "日本語で400文字以上600文字以内の説明文を書いてください。"
    "段落は3つに分け、箇条書きは使わないでください。"
    "前置きは書かず、本文だけを出力してください。"
)
DECODE_PROSE_TOKENS = 4096


def measure_decode(base, model, temperature=0.0, reps=3, gap=2.0, style="prose"):
    """decode probe を reps 回実行し、有効試行の中央値で返す。

    失敗試行も runs に残す（無申告の再送・棄却をしない）。
    """
    prompt = DECODE_PROSE_PROMPT if style == "prose" else DECODE_PROMPT
    max_tok = DECODE_PROSE_TOKENS if style == "prose" else DECODE_TOKENS
    runs = []
    for k in range(reps):
        r = chat_stream(base, model, prompt, max_tokens=max_tok, temperature=temperature)
        itls = r["itls"]
        valid = (r["state"] in ("ok", "empty", "truncated") and r["usage_available"]
                 and r["R_out"] is not None)
        runs.append({
            "state": r["state"],
            "valid": bool(valid),
            "usage_available": r["usage_available"],
            "R_out": r["R_out"],
            "gap_p50_ms": (statistics.median(itls) * 1000) if itls else None,
            "gap_p90_ms": ((sorted(itls)[int(len(itls) * 0.9) - 1] * 1000)
                            if len(itls) > 1 else ((itls[0] * 1000) if itls else None)),
            "tokens_per_chunk": round(r["completion_tokens"] / r["chunk_count"], 2)
            if (r["completion_tokens"] and itls) else None,
            "ttft_any": r["ttft_any"],
            "ttft_answer": r["ttft_answer"],
            "completion_tokens": r["completion_tokens"],
            "content_chars": r["content_chars"],
            "chars_per_s": round(r["content_chars"] / r["l"], 2)
            if (r["content_chars"] and r["l"]) else None,
            "content_chars_per_s": r["content_chars_per_s"],
            "chars_per_token": round(r["content_chars"] / r["completion_tokens"], 3)
            if (r["content_chars"] and r["completion_tokens"]) else None,
            "itl_samples": len(itls),
        })
        if k < reps - 1:
            time.sleep(gap)

    valid_runs = [x for x in runs if x["valid"]]

    def med(key):
        vals = [x[key] for x in valid_runs if x[key] is not None]
        return round(statistics.median(vals), 3) if vals else None

    if not valid_runs:
        raise RuntimeError(f"decode probe: 有効な試行なし (states={[x['state'] for x in runs]})")
    return {
        "style": style,
        "R_out_tok_per_s": round(med("R_out"), 1) if med("R_out") else None,
        "chars_per_s": med("chars_per_s"),
        "content_chars_per_s": med("content_chars_per_s"),
        "chars_per_token": med("chars_per_token"),
        "content_chars": med("content_chars"),
        "p50_inter_chunk_gap_ms": med("gap_p50_ms"),
        "p90_inter_chunk_gap_ms": med("gap_p90_ms"),
        "tokens_per_chunk": med("tokens_per_chunk"),
        "ttft_any_s": med("ttft_any"),
        "ttft_answer_s": med("ttft_answer"),
        "completion_tokens": med("completion_tokens"),
        "itl_samples": med("itl_samples"),
        "state": "ok",
        "requested": reps,
        "valid": len(valid_runs),
        "failed": reps - len(valid_runs),
        "probe_reps": len(valid_runs),
        "runs": runs,
    }


# ------------------------------------------------------------------ tasks


def run_tasks(base, model, task_ids, runs, temperature, save_full=True):
    results = []
    for tid in task_ids:
        task = TASK_BY_ID[tid]
        for i in range(runs):
            t_start = time.perf_counter()
            try:
                r = chat_stream(base, model, task["prompt"],
                                max_tokens=task.get("max_tokens", 1024),
                                temperature=temperature, timeout=900)
            except Exception as e:  # noqa: BLE001
                elapsed = time.perf_counter() - t_start
                results.append({"task": tid, "run": i, "passed": False, "state": "error",
                                "elapsed": round(elapsed, 3), "detail": str(e)[:300]})
                print(f"    {tid} run{i}: ERROR {elapsed:.1f}s ({str(e)[:60]})", flush=True)
                continue
            elapsed = r["total"]
            state = r["state"]
            # 最終回答が出ていない場合、length なら予算切れとして区別する
            if state == "empty" and r["finish_reason"] == "length":
                state = "budget_exhausted"
            if state != "ok":
                passed, detail = False, f"state={state} finish={r['finish_reason']} err={str(r['error'])[:120]}"
            elif not r["content"].strip():
                passed, detail = False, "empty content"
            else:
                try:
                    passed, detail = task["grader"](r["text"], r)
                    state = "pass" if passed else "wrong_answer"
                except Exception as e:  # noqa: BLE001
                    passed, detail, state = False, f"grader_error: {e}", "grader_error"
            results.append({
                "task": tid,
                "run": i,
                "passed": bool(passed),
                "state": state,
                "detail": detail,
                "elapsed": round(elapsed, 3),
                "ttft_any": round(r["ttft_any"], 3) if r["ttft_any"] else None,
                "ttft_answer": round(r["ttft_answer"], 3) if r["ttft_answer"] else None,
                "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"],
                "finish_reason": r["finish_reason"],
                "reasoning_chars": r["reasoning_chars"],
                "content_chars": r["content_chars"],
                "reasoning_repeat_ratio": r["reasoning_repeat_ratio"],
                "chars_per_s": round(r["content_chars"] / r["l"], 2)
                if (r["content_chars"] and r["l"]) else None,
                "content_chars_per_s": r["content_chars_per_s"],
                "l": round(r["l"], 3) if r["l"] else None,
                "done_seen": r["done_seen"],
                "parse_errors": r["parse_errors"],
                "usage_available": r["usage_available"],
                "degraded": r["degraded"],
                "usage": r["usage"],
                "reasoning_head": r["reasoning"][:2000],
                "reasoning_tail": r["reasoning"][-2000:],
                "reasoning": r["reasoning"] if save_full else None,
                "output": r["text"] if save_full else r["text"][:4000],
            })
            print(f"    {tid} run{i}: {state.upper()} {elapsed:.1f}s "
                  f"({str(detail)[:60]})", flush=True)
    return results


# ------------------------------------------------------------------ report


def summarize(model, speed, task_results):
    by_task = {}
    for r in task_results:
        by_task.setdefault(r["task"], []).append(r)
    per_task = []
    for tid, rs in by_task.items():
        times = [r["elapsed"] for r in rs if r["elapsed"] is not None]
        passes = sum(1 for r in rs if r["passed"])
        per_task.append({
            "task": tid,
            "pass": f"{passes}/{len(rs)}",
            "rate": passes / len(rs),
            "mean_time_s": round(statistics.mean(times), 2) if times else None,
        })
    total_runs = len(task_results)
    passes = sum(1 for r in task_results if r["passed"])
    # 失敗も含めた全試行の時間（欠測にしない）
    times = [r["elapsed"] for r in task_results if r["elapsed"] is not None]
    success_rate = passes / total_runs if total_runs else 0
    # 正常応答に限定した品質（信頼性エラーを分母から除く）
    quality_states = {"pass", "wrong_answer", "format_violation", "budget_exhausted", "empty"}
    quality_denom = sum(1 for r in task_results if r.get("state") in quality_states)
    quality_rate = (passes / quality_denom) if quality_denom else None
    grader_errors = sum(1 for r in task_results if r.get("state") == "grader_error")
    mean_time = statistics.mean(times) if times else None
    # 観測消費時間あたりの成功（specs/metrics.md §5）
    ttp = (sum(times) / passes) if (times and passes > 0) else None
    states = {}
    for r in task_results:
        states[r.get("state", "unknown")] = states.get(r.get("state", "unknown"), 0) + 1
    # 不確実性: attempt 単位（クラスタ内反復のため過小評価）と task 単位
    ci = wilson_ci(passes, total_runs)
    task_all_pass = sum(1 for p in per_task if p["rate"] == 1.0)
    task_ci = wilson_ci(task_all_pass, len(per_task))
    ccps = [r["content_chars_per_s"] for r in task_results
            if r.get("content_chars_per_s") and r.get("state") in ("pass", "wrong_answer")]
    cps = [r["chars_per_s"] for r in task_results
           if r.get("chars_per_s") and r.get("state") in ("pass", "wrong_answer")]
    repeats = [r["reasoning_repeat_ratio"] for r in task_results
               if r.get("reasoning_repeat_ratio") is not None]
    return {
        "model": model,
        "speed": speed,
        "tasks": task_results,
        "per_task": per_task,
        "summary": {
            "task_runs": total_runs,
            "task_passes": passes,
            "success_rate": round(success_rate, 4),
            "success_rate_ci95": [round(x, 4) for x in ci] if ci else None,
            "task_pass_rate": round(task_all_pass / len(per_task), 4) if per_task else None,
            "task_pass_ci95": [round(x, 4) for x in task_ci] if task_ci else None,
            "quality_rate": round(quality_rate, 4) if quality_rate is not None else None,
            "grader_errors": grader_errors,
            "degraded": grader_errors > 0,
            "mean_task_time_s": round(mean_time, 2) if mean_time else None,
            "time_to_pass_s": round(ttp, 2) if ttp else None,
            "mean_reasoning_repeat": round(statistics.mean(repeats), 3) if repeats else None,
            "mean_chars_per_s": round(statistics.mean(cps), 2) if cps else None,
            "mean_content_chars_per_s": round(statistics.mean(ccps), 2) if ccps else None,
            "states": states,
        },
    }


def fmt(v, digits=1):
    if v is None:
        return "-"
    return f"{v:,.{digits}f}"


def markdown(results):
    lines = ["# 日本語 LLM ベンチマーク", "",
             f"実行: {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}", ""]
    for res in results:
        s = res["speed"] or {}
        sm = res["summary"]
        lines += [f"## {res['model']}", "",
                  "| 指標 | 値 |", "|---|---|",
                  f"| Prefill | {fmt(s.get('prefill_tps'), 0)} tok/s"
                  + (f"（95%CI {fmt(s['prefill_ci95'][0], 0)}–{fmt(s['prefill_ci95'][1], 0)}）" if s.get("prefill_ci95") else "")
                  + f" (TTFT_any 全対回帰, R²={fmt(s.get('r2'), 3)}, {s.get('prefill_blocks', '-')}ブロック×{s.get('probe_reps', '-')}回) |",
                  f"| Prefill（ロバスト） | {fmt(s.get('prefill_tps_robust'), 0)} tok/s (Theil-Sen 傾きの中央値) |",
                  f"| TTFT ジッタ | p50 {fmt(s.get('ttft_p50_s'), 3)}s / p95 {fmt(s.get('ttft_p95_s'), 3)}s（p95/p50={fmt(s.get('ttft_jitter_p95_p50'), 2)}倍） |",
                  f"| 日本語トークン効率 | {fmt(s.get('tokens_per_char'), 3)} tok/文字（入力。1文字1トークン超は日本語の割高） |",
                  f"| Decode (client観測) | {fmt(s.get('decode_tps'), 1)} tok/s"
                  + (f" / 実効 {fmt(s.get('decode_chars_per_s'), 1)} 文字/s・本文のみ "
                     f"{fmt(s.get('decode_content_chars_per_s'), 1)} 文字/s（{s.get('decode_style')}、"
                     f"{fmt(s.get('decode_chars_per_token'), 2)} 文字/token）"
                     if s.get("decode_chars_per_s") else "")
                  + f" (p50 inter-chunk gap {fmt(s.get('p50_itl_ms'), 1)} ms, n={s.get('probe_reps', '-')}中央値) |",
                  f"| Task Time | {fmt(sm.get('mean_task_time_s'), 1)} s (全試行平均, {sm['task_runs']}回) |",
                  f"| Success Rate | {sm['success_rate'] * 100:.0f}% ({sm['task_passes']}/{sm['task_runs']})"
                  + (f" / 正常応答内 {sm['quality_rate'] * 100:.0f}%" if sm.get("quality_rate") is not None else "")
                  + " |",
                  f"| Task 完全合格 | {sm.get('task_pass_rate', 0) * 100:.0f}%"
                  + (f" (95%CI {sm['task_pass_ci95'][0] * 100:.0f}–{sm['task_pass_ci95'][1] * 100:.0f}%)" if sm.get("task_pass_ci95") else "") + " |",
                  f"| TTP (観測時間/成功) | {fmt(sm.get('time_to_pass_s'), 1)} s |",
                  ""]
        if sm.get("degraded"):
            lines.append(f"- **degraded**: grader_error {sm['grader_errors']}件（測定不能）")
        if sm.get("success_rate_ci95"):
            lines.append(
                f"- 成功率の Wilson 95%CI: {sm['success_rate_ci95'][0] * 100:.0f}–{sm['success_rate_ci95'][1] * 100:.0f}%"
                "（naive iid。反復はクラスタ内反復なので過小評価）")
        if sm.get("mean_chars_per_s") is not None:
            lines.append(f"- 日本語出力速度（本文の文字/s、reasoning込みの実効）: "
                         f"{fmt(sm['mean_chars_per_s'], 1)} 文字/s")
            if sm.get("mean_content_chars_per_s") is not None:
                lines.append(f"- 日本語生成速度（本文区間のみ）: "
                             f"{fmt(sm['mean_content_chars_per_s'], 1)} 文字/s")
        if sm.get("mean_reasoning_repeat") is not None:
            lines.append(f"- reasoning 行重複率（平均）: {sm['mean_reasoning_repeat']:.2f}（高いほど反復・degeneration）")
        if sm.get("states"):
            lines.append("- 状態: " + ", ".join(f"{k}={v}" for k, v in sorted(sm["states"].items())))
        if s.get("cold_start_ttft_s") is not None:
            lines.append(f"- コールドスタートTTFT（初回モデルロード込み）: {fmt(s['cold_start_ttft_s'], 1)} s")
        if s.get("intercept_s") is not None:
            lines.append(f"- 固定オーバーヘッド（切片）: {fmt(s['intercept_s'], 3)} s")
        if s.get("p90_itl_ms") is not None:
            lines.append(f"- ITL p90: {fmt(s['p90_itl_ms'], 1)} ms（ばらつきの目安）")
        lines += ["", "| タスク | 合格 | 成功率 | 平均時間 |", "|---|---|---|---|"]
        for pt in sorted(res["per_task"], key=lambda x: -x["rate"]):
            lines.append(f"| {pt['task']} | {pt['pass']} | {pt['rate'] * 100:.0f}% | "
                         f"{fmt(pt['mean_time_s'], 1)} s |")
        lines.append("")
        fails = [t for t in res["tasks"] if not t["passed"]]
        if fails:
            lines.append("失敗の内訳:")
            for f in fails[:12]:
                lines.append(f"- {f['task']} run{f['run']}: {str(f.get('detail'))[:160]}")
            lines.append("")
    return "\n".join(lines)


def _git_commit():
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=os.path.dirname(os.path.abspath(__file__)),
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def dataset_hash(task_ids):
    payload = [[t, TASK_BY_ID[t]["prompt"], TASK_BY_ID[t].get("max_tokens")] for t in task_ids]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def build_manifest(args, model, task_ids, run_id):
    meta = {}
    for kv in args.sut_meta:
        k, _, v = kv.partition("=")
        if k.strip():
            meta[k.strip()] = v.strip()
    commit = _git_commit()
    return {
        "schema_version": "0.1",
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "sut": {
            "name": model,
            "model_revision": meta.get("model_revision"),
            "quantization": meta.get("quantization"),
            "tokenizer_hash": meta.get("tokenizer_hash"),
            "chat_template_hash": meta.get("chat_template_hash"),
            "engine": {"name": meta.get("engine", "unknown"),
                       "version": meta.get("engine_version", "unknown"),
                       "args": []},
            "gpu": {"type": meta.get("gpu", "unknown"),
                    "count": int(meta.get("gpu_count", 0) or 0),
                    "driver": meta.get("driver")},
            "parallelism": {k: int(v) for k, v in meta.items()
                            if k in ("tp", "ep", "dp") and str(v).isdigit()},
            "speculative": {"method": meta["speculative"]} if "speculative" in meta else None,
            "sampling": {"temperature": args.temperature, "top_p": None, "seed": None},
            "cache": {"mode": args.cache_mode},
            "context_limit": int(meta["context_limit"]) if str(meta.get("context_limit", "")).isdigit() else None,
        },
        "endpoint": {
            "base_url": args.base_url.split("/v1")[0],
            "client_version": commit,
            "network": meta.get("network", "unknown"),
        },
        "protocol": {
            "scenario": args.scenario,
            "repetitions": args.runs,
            "timeout_s": 900,
            "retry": "off",
            "max_tokens_policy": "per-task (tasks.py)",
            "seed": None,
        },
        "dataset": {"name": "ja_smoke_v1", "hash": dataset_hash(task_ids),
                    "task_count": len(task_ids)},
        "grader": {"version": commit, "contract": "strict_v1"},
    }


def write_raw(out_dir, all_results, manifest):
    """SUT ごとに manifest を分けて保存し、attempt には sut 名を付ける。"""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", manifest["sut"]["name"])[:80]
    with open(os.path.join(out_dir, f"manifest_{safe}.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    if len(all_results) == 1:
        with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "attempts.jsonl"), "w", encoding="utf-8") as f:
        for res in all_results:
            for t in res["tasks"]:
                rec = {"run_id": manifest["run_id"], "sut": res["model"], **t}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ main


def main():
    ap = argparse.ArgumentParser(description="日本語 LLM ベンチマーク")
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--model", required=True, help="カンマ区切りで複数指定可")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--tasks", default="all")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--prefill-lengths", default="500,2000,8000",
                    help="プロンプト長（文字数）のカンマ区切り")
    ap.add_argument("--no-speed", action="store_true")
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--api-key", default=None, help="Authorization: Bearer に使うキー")
    ap.add_argument("--sut-meta", action="append", default=[], metavar="K=V",
                    help="SUT のメタデータ（quantization=EXL3-4bpw, engine=vllm など）")
    ap.add_argument("--cache-mode", default="unknown", choices=["off", "on", "unknown"])
    ap.add_argument("--scenario", default="S1", choices=["S1", "S2", "S3"])
    ap.add_argument("--probe-reps", type=int, default=3,
                    help="prefill/decode probe の反復回数（中央値を採用）")
    ap.add_argument("--prefill-blocks", type=int, default=3,
                    help="prefill probe のブロック数（ブロック内で長さ順をランダム化）")
    ap.add_argument("--probe-gap", type=float, default=2.0,
                    help="probe 反復間の idle 秒")
    ap.add_argument("--decode-style", default="prose", choices=["prose", "numbers"],
                    help="decode probe の出力形式（prose=日本語400-600字 / numbers=数字列）")
    ap.add_argument("--no-full-text", action="store_true",
                    help="生ログの回答全文・reasoning 全文を保存しない（既定は保存）")
    args = ap.parse_args()
    if args.api_key:
        global _API_KEY
        _API_KEY = args.api_key

    models = [m.strip() for m in args.model.split(",") if m.strip()]
    task_ids = [t.strip() for t in args.tasks.split(",")] if args.tasks != "all" else [t["id"] for t in TASKS]
    unknown = [t for t in task_ids if t not in TASK_BY_ID]
    if unknown:
        sys.exit(f"unknown task ids: {unknown}")

    out_dir = args.out or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "results", datetime.now().strftime("%Y%m%dT%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)

    all_results = []
    for model in models:
        print(f"\n=== {model} ===", flush=True)
        cold = None
        if not args.no_warmup:
            print("  warmup (モデルロード完了待ち)...", flush=True)
            cold, attempts = wait_ready(args.base_url, model, args.temperature)
            cold = round(cold, 2) if cold else None
            chat_stream(args.base_url, model, build_prompt(1500), max_tokens=8,
                        temperature=args.temperature, timeout=900)
            print(f"  コールドスタートTTFT: {cold} s ({attempts}回試行)", flush=True)

        speed = None
        if not args.no_speed:
            lengths = [int(x) for x in args.prefill_lengths.split(",")]
            print(f"  prefill probe (chars={lengths})...", flush=True)
            pre = measure_prefill(args.base_url, model, lengths, args.temperature,
                                   args.probe_reps, args.probe_gap, blocks=args.prefill_blocks)
            print(f"    prefill: {fmt(pre['prefill_tps'], 0)} tok/s (R2={fmt(pre['r2'], 3)})", flush=True)
            print("  decode probe...", flush=True)
            dec = measure_decode(args.base_url, model, args.temperature,
                                  args.probe_reps, args.probe_gap, style=args.decode_style)
            print(f"    R_out: {fmt(dec['R_out_tok_per_s'], 1)} tok/s "
                  f"(inter-chunk gap p50 {fmt(dec['p50_inter_chunk_gap_ms'], 1)} ms, "
                  f"{fmt(dec['tokens_per_chunk'], 2)} tok/chunk)", flush=True)
            speed = {"prefill": pre, "decode": dec,
                     "prefill_tps": pre["prefill_tps"],
                     "prefill_ci95": pre.get("prefill_ci95"),
                     "prefill_blocks": pre.get("blocks"),
                     "intercept_s": pre["intercept_s"],
                     "r2": pre["r2"],
                     "prefill_samples": pre["samples"],
                     "tokens_per_char": pre["mean_tokens_per_char"],
                     "probe_reps": dec["probe_reps"],
                     "decode_tps": dec["R_out_tok_per_s"],
                     "decode_style": dec.get("style"),
                     "decode_chars_per_s": dec.get("chars_per_s"),
                     "decode_content_chars_per_s": dec.get("content_chars_per_s"),
                     "decode_chars_per_token": dec.get("chars_per_token"),
                     "p50_itl_ms": dec["p50_inter_chunk_gap_ms"],
                     "p90_itl_ms": dec["p90_inter_chunk_gap_ms"],
                     "tokens_per_chunk": dec["tokens_per_chunk"],
                     "cold_start_ttft_s": cold}

        print(f"  tasks: {len(task_ids)} x {args.runs} runs", flush=True)
        task_results = run_tasks(args.base_url, model, task_ids, args.runs, args.temperature,
                                  save_full=not args.no_full_text)
        res = summarize(model, speed, task_results)
        all_results.append(res)

        with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        manifest = build_manifest(args, model, task_ids, os.path.basename(out_dir))
        write_raw(out_dir, all_results, manifest)
        with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(markdown(all_results))
        print(f"  saved -> {out_dir}", flush=True)

    print("\n" + markdown(all_results))


if __name__ == "__main__":
    main()
