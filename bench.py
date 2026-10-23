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
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone

import requests

from tasks import TASKS, TASK_BY_ID

DEFAULT_BASE = "https://127.0.0.1:8000/v1"


# ------------------------------------------------------------------ HTTP


def _sse_events(resp):
    """SSE のイベント境界（空行）で区切って返す。行分割で JSON が壊れるのを防ぐ。"""
    buf = b""
    for chunk in resp.iter_content(chunk_size=8192):
        if not chunk:
            continue
        buf += chunk
        while b"\n\n" in buf:
            event, buf = buf.split(b"\n\n", 1)
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


def chat_stream(base, model, prompt, max_tokens, temperature=0.0, timeout=600):
    """1リクエストをストリーミングで実行し、計測値を返す。"""
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
    ttft = None
    chunk_times = []
    content_parts = []
    reasoning_parts = []
    usage = {}
    last_err = None
    for attempt in range(2):
        try:
            with requests.post(url, json=body, stream=True, timeout=timeout) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                for event in _sse_events(r):
                    obj = _parse_event(event)
                    if obj is None:
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    for ch in obj.get("choices") or []:
                        d = ch.get("delta") or {}
                        piece = d.get("content")
                        think = d.get("reasoning_content")
                        if piece or think:
                            now = time.perf_counter()
                            if ttft is None:
                                ttft = now - t0
                            chunk_times.append(now - t0)
                            if piece:
                                content_parts.append(piece)
                            if think:
                                reasoning_parts.append(think)
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt == 1:
                raise
            t0 = time.perf_counter()
            ttft, chunk_times, content_parts, reasoning_parts, usage = None, [], [], [], {}

    total = time.perf_counter() - t0
    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    # 採点は最終回答(content)のみ。reasoning 内のコードで偶然通るのを防ぐ
    text = content
    completion = usage.get("completion_tokens") or len(chunk_times)
    prompt_tokens = usage.get("prompt_tokens")
    itls = [b - a for a, b in zip(chunk_times, chunk_times[1:])]
    decode_span = (chunk_times[-1] - chunk_times[0]) if len(chunk_times) > 1 else None
    # チャンクには複数トークンがまとまって届くことがある（実測 4.3 tok/chunk）ため、
    # チャンク数ではなく usage の completion_tokens で割る
    decode_tps = ((completion - 1) / decode_span) if (decode_span and completion > 1) else None
    return {
        "text": text,
        "content": content,
        "reasoning": reasoning,
        "ttft": ttft,
        "total": total,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion,
        "itls": itls,
        "decode_tps": decode_tps,
        "usage": usage,
        "error": str(last_err) if last_err else None,
    }


# ------------------------------------------------------------------ speed


FILLER = (
    "当社は1978年に大阪で創業し、当初は金属部品の加工を主力事業としていた。"
    "1990年代には海外取引先との連携を強め、東南アジアに現地法人を設立した。"
    "2005年には品質管理部門を独立させ、全社的な検査体制を再構築した。"
    "2011年以降は環境負荷低減を経営課題と位置づけ、設備更新を段階的に進めた。"
    "2016年からはIoTセンサーを導入し、生産ラインの稼働データを可視化している。"
)


import uuid


def build_prompt(chars: int) -> str:
    # 先頭にユニークな salt を置き、prefix caching によるヒットを防ぐ
    # （FILLER の繰り返しだと長いプロンプトが短いプロンプトの prefix を共有してしまう）
    head = f"資料番号 {uuid.uuid4().hex}。以下は社内文書です。内容を一文で要約してください。\n\n"
    reps = max(1, chars // len(FILLER) + 1)
    body = (FILLER * reps)[:chars]
    return head + body


def wait_ready(base, model, temperature=0.0, max_wait=1800):
    """モデルが実際に応答できるまで待つ。TTFT が 3 秒未満になったら準備完了とみなす。

    モデル入れ替え式サーバーでは、リクエスト受付自体は返るがロード中のことがあり、
    そのまま計測すると TTFT が数百秒になって prefill が負の傾きになる。
    """
    t_start = time.perf_counter()
    cold = None
    attempts = 0
    while time.perf_counter() - t_start < max_wait:
        attempts += 1
        w = chat_stream(base, model, "こんにちは。短く返して。", max_tokens=8,
                        temperature=temperature, timeout=900)
        t = w["ttft"]
        if cold is None:
            cold = t
        if t is not None and t < 3.0:
            return cold, attempts
        time.sleep(10)
    raise RuntimeError(f"{model}: モデルが {max_wait}s 以内に準備完了しなかった")


def measure_prefill(base, model, lengths, temperature=0.0):
    """TTFT = a + b * prompt_tokens を最小二乗で当てはめ、1/b を prefill tok/s とする。"""
    # 最初の1本は捨てる（キャッシュ初期化などを避ける）
    chat_stream(base, model, build_prompt(min(lengths)), max_tokens=8,
                temperature=temperature, timeout=900)
    time.sleep(0.5)
    samples = []
    for chars in lengths:
        prompt = build_prompt(chars)
        for attempt in range(3):
            r = chat_stream(base, model, prompt, max_tokens=8, temperature=temperature, timeout=600)
            if r["ttft"] is not None and r["prompt_tokens"] and r["ttft"] < 30:
                break
            time.sleep(5)
        else:
            raise RuntimeError(f"prefill probe failed ({chars} chars): {r['error']}")
        samples.append({"chars": chars, "prompt_tokens": r["prompt_tokens"],
                        "ttft": round(r["ttft"], 4)})
        time.sleep(0.3)

    xs = [s["prompt_tokens"] for s in samples]
    ys = [s["ttft"] for s in samples]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        raise RuntimeError("prefill probe: prompt lengths identical")
    b = sxy / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return {
        "prefill_tps": (1 / b) if b > 0 else None,
        "intercept_s": a,
        "r2": r2,
        "samples": samples,
        "note": "TTFTの傾き法。切片=ネットワーク+キュー待ち+固定オーバーヘッド",
    }


DECODE_PROMPT = "1から200までの整数を1行に1つずつ、何も付け加えずに出力してください。"
DECODE_TOKENS = 256


def measure_decode(base, model, temperature=0.0):
    r = chat_stream(base, model, DECODE_PROMPT, max_tokens=DECODE_TOKENS, temperature=temperature)
    if not r["itls"]:
        raise RuntimeError(f"decode probe produced no tokens: {r['error']}")
    itls = r["itls"]
    p50 = statistics.median(itls)
    p90 = sorted(itls)[int(len(itls) * 0.9) - 1] if len(itls) > 1 else itls[0]
    span = r["total"] - r["ttft"] if r["ttft"] else None
    tokens = r["completion_tokens"]
    return {
        "decode_tps": r["decode_tps"],
        "decode_tps_p50": (1 / p50) if p50 > 0 else None,
        "p50_itl_ms": p50 * 1000,
        "p90_itl_ms": p90 * 1000,
        "token_itl_ms": (span / (tokens - 1) * 1000) if (span and tokens and tokens > 1) else None,
        "tokens_per_chunk": round(tokens / len(itls), 2) if itls else None,
        "ttft": r["ttft"],
        "completion_tokens": tokens,
        "itl_samples": len(itls),
    }


# ------------------------------------------------------------------ tasks


def run_tasks(base, model, task_ids, runs, temperature):
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
                results.append({"task": tid, "run": i, "passed": False, "elapsed": None,
                                "error": str(e)[:300]})
                continue
            elapsed = time.perf_counter() - t_start
            if not r["content"].strip():
                passed, detail = False, f"最終回答なし（reasoning {r['completion_tokens']} tok で max_tokens 到達）"
            else:
                passed, detail = task["grader"](r["text"], r)
            results.append({
                "task": tid,
                "run": i,
                "passed": bool(passed),
                "detail": detail,
                "elapsed": round(elapsed, 3),
                "ttft": round(r["ttft"], 3) if r["ttft"] else None,
                "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"],
                "output": r["text"][:1200],
            })
            print(f"    {tid} run{i}: {'PASS' if passed else 'FAIL'} {elapsed:.1f}s "
                  f"({detail[:60]})", flush=True)
    return results


# ------------------------------------------------------------------ report


def summarize(model, speed, task_results):
    by_task = {}
    for r in task_results:
        by_task.setdefault(r["task"], []).append(r)
    per_task = []
    for tid, rs in by_task.items():
        times = [r["elapsed"] for r in rs if r["elapsed"]]
        passes = sum(1 for r in rs if r["passed"])
        per_task.append({
            "task": tid,
            "pass": f"{passes}/{len(rs)}",
            "rate": passes / len(rs),
            "mean_time_s": round(statistics.mean(times), 2) if times else None,
        })
    total_runs = len(task_results)
    passes = sum(1 for r in task_results if r["passed"])
    times = [r["elapsed"] for r in task_results if r["elapsed"]]
    success_rate = passes / total_runs if total_runs else 0
    mean_time = statistics.mean(times) if times else None
    ttp = (mean_time / success_rate) if (mean_time and success_rate > 0) else None
    return {
        "model": model,
        "speed": speed,
        "tasks": task_results,
        "per_task": per_task,
        "summary": {
            "task_runs": total_runs,
            "task_passes": passes,
            "success_rate": round(success_rate, 4),
            "mean_task_time_s": round(mean_time, 2) if mean_time else None,
            "time_to_pass_s": round(ttp, 2) if ttp else None,
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
                  f"| Prefill | {fmt(s.get('prefill_tps'), 0)} tok/s (傾き法, R²={fmt(s.get('r2'), 3)}) |",
                  f"| Decode | {fmt(s.get('decode_tps'), 1)} tok/s (p50 ITL {fmt(s.get('p50_itl_ms'), 1)} ms) |",
                  f"| Task Time | {fmt(sm.get('mean_task_time_s'), 1)} s (平均, {sm['task_runs']}回) |",
                  f"| Success Rate | {sm['success_rate'] * 100:.0f}% ({sm['task_passes']}/{sm['task_runs']}) |",
                  f"| Time-to-Pass | {fmt(sm.get('time_to_pass_s'), 1)} s |",
                  ""]
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
    args = ap.parse_args()

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
            pre = measure_prefill(args.base_url, model, lengths, args.temperature)
            print(f"    prefill: {fmt(pre['prefill_tps'], 0)} tok/s (R2={fmt(pre['r2'], 3)})", flush=True)
            print("  decode probe...", flush=True)
            dec = measure_decode(args.base_url, model, args.temperature)
            print(f"    decode: {fmt(dec['decode_tps'], 1)} tok/s", flush=True)
            speed = {"prefill": pre, "decode": dec,
                     "prefill_tps": pre["prefill_tps"],
                     "intercept_s": pre["intercept_s"],
                     "r2": pre["r2"],
                     "prefill_samples": pre["samples"],
                     "decode_tps": dec["decode_tps"],
                     "decode_tps_p50": dec["decode_tps_p50"],
                     "p50_itl_ms": dec["p50_itl_ms"],
                     "p90_itl_ms": dec["p90_itl_ms"],
                     "cold_start_ttft_s": cold}

        print(f"  tasks: {len(task_ids)} x {args.runs} runs", flush=True)
        task_results = run_tasks(args.base_url, model, task_ids, args.runs, args.temperature)
        res = summarize(model, speed, task_results)
        all_results.append(res)

        with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(markdown(all_results))
        print(f"  saved -> {out_dir}", flush=True)

    print("\n" + markdown(all_results))


if __name__ == "__main__":
    main()
