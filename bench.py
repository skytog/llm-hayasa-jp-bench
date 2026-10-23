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
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

from tasks import TASKS, TASK_BY_ID

DEFAULT_BASE = "https://127.0.0.1:8000/v1"


# ------------------------------------------------------------------ HTTP


def _sse_events(resp):
    """SSE のイベント境界（空行）で区切って返す。CRLF も扱う。"""
    buf = b""
    for chunk in resp.iter_content(chunk_size=8192):
        if not chunk:
            continue
        buf += chunk
        buf = buf.replace(b"\r\n", b"\n")
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


def chat_stream(base, model, prompt, max_tokens, temperature=0.0, timeout=600, retry=0):
    """1リクエストをストリーミングで実行し、観測契約（specs/metrics.md §1）に沿った値を返す。

    失敗しても elapsed（失敗確定までの時間）を必ず返す。
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
    ttft_any = None
    ttft_answer = None
    chunk_times = []
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
            with requests.post(url, json=body, stream=True, timeout=timeout) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                for event in _sse_events(r):
                    if "[DONE]" in event:
                        done_seen = True
                        continue
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
                        think = d.get("reasoning_content")
                        if piece or think:
                            now = time.perf_counter() - t0
                            if ttft_any is None:
                                ttft_any = now
                            if piece and ttft_answer is None:
                                ttft_answer = now
                            chunk_times.append(now)
                            if piece:
                                content_parts.append(piece)
                            if think:
                                reasoning_parts.append(think)
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
    # 採点は最終回答(content)のみ。reasoning 内のコードで偶然通るのを防ぐ
    text = content
    completion = usage.get("completion_tokens")
    prompt_tokens = usage.get("prompt_tokens")
    itls = [b - a for a, b in zip(chunk_times, chunk_times[1:])]
    span = (chunk_times[-1] - chunk_times[0]) if len(chunk_times) > 1 else None
    # クライアント観測出力レート。usage が無い場合 C は不明で、チャンク数で代用しない
    R_out = ((completion - 1) / span) if (span and completion and completion > 1) else None

    if state == "ok":
        if not (done_seen or finish_reason):
            state = "truncated"
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
        "total": total,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion,
        "itls": itls,
        "R_out": R_out,
        "usage": usage,
        "finish_reason": finish_reason,
        "done_seen": done_seen,
        "parse_errors": parse_errors,
        "state": state,
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
    if r["state"] != "ok":
        raise RuntimeError(f"decode probe state={r['state']}: {r['error']}")
    itls = r["itls"]
    tokens = r["completion_tokens"]
    gap_p50 = statistics.median(itls) if itls else None
    gap_p90 = (sorted(itls)[int(len(itls) * 0.9) - 1] if len(itls) > 1 else (itls[0] if itls else None))
    return {
        "R_out_tok_per_s": r["R_out"],
        "p50_inter_chunk_gap_ms": gap_p50 * 1000 if gap_p50 else None,
        "p90_inter_chunk_gap_ms": gap_p90 * 1000 if gap_p90 else None,
        "tokens_per_chunk": round(tokens / len(itls), 2) if (tokens and itls) else None,
        "ttft_any_s": r["ttft_any"],
        "ttft_answer_s": r["ttft_answer"],
        "completion_tokens": tokens,
        "itl_samples": len(itls),
        "state": r["state"],
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
                elapsed = time.perf_counter() - t_start
                results.append({"task": tid, "run": i, "passed": False, "state": "error",
                                "elapsed": round(elapsed, 3), "detail": str(e)[:300]})
                print(f"    {tid} run{i}: ERROR {elapsed:.1f}s ({str(e)[:60]})", flush=True)
                continue
            elapsed = r["total"]
            state = r["state"]
            if state != "ok":
                passed, detail = False, f"state={state} finish={r['finish_reason']} err={str(r['error'])[:120]}"
            elif not r["content"].strip():
                passed, detail = False, "empty content"
                if r["finish_reason"] == "length":
                    state = "budget_exhausted"
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
                "output": r["text"][:4000],
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
    return {
        "model": model,
        "speed": speed,
        "tasks": task_results,
        "per_task": per_task,
        "summary": {
            "task_runs": total_runs,
            "task_passes": passes,
            "success_rate": round(success_rate, 4),
            "quality_rate": round(quality_rate, 4) if quality_rate is not None else None,
            "grader_errors": grader_errors,
            "degraded": grader_errors > 0,
            "mean_task_time_s": round(mean_time, 2) if mean_time else None,
            "time_to_pass_s": round(ttp, 2) if ttp else None,
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
                  f"| Prefill | {fmt(s.get('prefill_tps'), 0)} tok/s (傾き法, R²={fmt(s.get('r2'), 3)}) |",
                  f"| Decode (client観測) | {fmt(s.get('decode_tps'), 1)} tok/s (p50 inter-chunk gap {fmt(s.get('p50_itl_ms'), 1)} ms) |",
                  f"| Task Time | {fmt(sm.get('mean_task_time_s'), 1)} s (全試行平均, {sm['task_runs']}回) |",
                  f"| Success Rate | {sm['success_rate'] * 100:.0f}% ({sm['task_passes']}/{sm['task_runs']})"
                  + (f" / 正常応答内 {sm['quality_rate'] * 100:.0f}%" if sm.get("quality_rate") is not None else "") + " |",
                  f"| TTP (観測時間/成功) | {fmt(sm.get('time_to_pass_s'), 1)} s |",
                  ""]
        if sm.get("degraded"):
            lines.append(f"- **degraded**: grader_error {sm['grader_errors']}件（測定不能）")
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
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "attempts.jsonl"), "w", encoding="utf-8") as f:
        for res in all_results:
            for t in res["tasks"]:
                rec = {"run_id": manifest["run_id"], "model": res["model"], **t}
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
    ap.add_argument("--sut-meta", action="append", default=[], metavar="K=V",
                    help="SUT のメタデータ（quantization=EXL3-4bpw, engine=vllm など）")
    ap.add_argument("--cache-mode", default="unknown", choices=["off", "on", "unknown"])
    ap.add_argument("--scenario", default="S1", choices=["S1", "S2", "S3"])
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
            print(f"    R_out: {fmt(dec['R_out_tok_per_s'], 1)} tok/s "
                  f"(inter-chunk gap p50 {fmt(dec['p50_inter_chunk_gap_ms'], 1)} ms, "
                  f"{fmt(dec['tokens_per_chunk'], 2)} tok/chunk)", flush=True)
            speed = {"prefill": pre, "decode": dec,
                     "prefill_tps": pre["prefill_tps"],
                     "intercept_s": pre["intercept_s"],
                     "r2": pre["r2"],
                     "prefill_samples": pre["samples"],
                     "decode_tps": dec["R_out_tok_per_s"],
                     "p50_itl_ms": dec["p50_inter_chunk_gap_ms"],
                     "p90_itl_ms": dec["p90_inter_chunk_gap_ms"],
                     "tokens_per_chunk": dec["tokens_per_chunk"],
                     "cold_start_ttft_s": cold}

        print(f"  tasks: {len(task_ids)} x {args.runs} runs", flush=True)
        task_results = run_tasks(args.base_url, model, task_ids, args.runs, args.temperature)
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
