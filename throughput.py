#!/usr/bin/env python3
"""同時リクエストのスループット測定.

並列数 c を変えて、合計 tok/s・1本あたりの decode・TTFT 分布・完了時間を見る。
単一ストリームのレイテンシ（bench.py）とは別物で、こちらは「捌ける量」を測る。

使い方:
  python3 throughput.py --model X --concurrency 1,2,4,8,16 --rounds 2
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from bench import DEFAULT_BASE, chat_stream

PROMPT = "1から200までの整数を1行に1つずつ、何も付け加えずに出力してください。"
MAX_TOKENS = 256


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))
    return s[idx]


def _safe_chat(base, model, temperature):
    try:
        r = chat_stream(base, model, PROMPT, MAX_TOKENS, temperature, 900)
        return {"state": r["state"], "error": r.get("error"),
                "completion_tokens": r["completion_tokens"],
                "usage_available": r["usage_available"],
                "ttft": r["ttft_any"], "total": r["total"], "R_out": r["R_out"]}
    except Exception as e:  # noqa: BLE001
        return {"state": "error", "error": str(e)[:160], "completion_tokens": None,
                "usage_available": False, "ttft": None, "total": 0.0, "R_out": None}


def run_level(base, model, conc, temperature):
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as pool:
        futures = [pool.submit(_safe_chat, base, model, temperature) for _ in range(conc)]
        results = [f.result() for f in futures]
    wall = time.perf_counter() - t0

    ok_states = ("ok", "empty", "truncated")
    ok = [r for r in results if r["state"] in ok_states]
    errors = [r for r in results if r["state"] not in ok_states]
    with_usage = [r for r in ok if r["usage_available"] and r["completion_tokens"]]
    coverage = round(len(with_usage) / len(ok), 3) if ok else None
    total_tokens = sum(r["completion_tokens"] for r in with_usage)
    ttfts = [r["ttft"] for r in ok if r["ttft"]]
    decodes = [r["R_out"] for r in ok if r.get("R_out")]
    totals = [r["total"] for r in ok]
    return {
        "concurrency": conc,
        "requests_ok": len(ok),
        "errors": len(errors),
        "error_sample": errors[0]["error"] if errors else None,
        "wall_s": round(wall, 3),
        "total_completion_tokens": total_tokens,
        "agg_tps": round(total_tokens / wall, 1) if wall > 0 else None,
        "token_coverage": coverage,
        "per_stream_decode_p50": round(statistics.median(decodes), 1) if decodes else None,
        "ttft_p50": round(statistics.median(ttfts), 3) if ttfts else None,
        "ttft_p90": round(pct(ttfts, 90), 3) if ttfts else None,
        "latency_p50": round(statistics.median(totals), 3) if totals else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--model", required=True)
    ap.add_argument("--concurrency", default="1,2,4,8,16")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    levels = [int(x) for x in args.concurrency.split(",")]
    out_dir = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "results", "throughput-" + datetime.now().strftime("%Y%m%dT%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)

    print(f"model: {args.model}  prompt={len(PROMPT)} chars  max_tokens={MAX_TOKENS}", flush=True)
    rows = []
    for conc in levels:
        for rnd in range(args.rounds):
            row = run_level(args.base_url, args.model, conc, args.temperature)
            row["round"] = rnd
            rows.append(row)
            print(f"  c={conc:<3d} r{rnd}: agg={row['agg_tps']} tok/s  "
                  f"per-stream={row['per_stream_decode_p50']}  ttft_p50={row['ttft_p50']}s  "
                  f"lat_p50={row['latency_p50']}s  wall={row['wall_s']}s"
                  + (f"  ERR={row['errors']}" if row["errors"] else ""), flush=True)

    with open(os.path.join(out_dir, "throughput.json"), "w", encoding="utf-8") as f:
        json.dump({"model": args.model, "rows": rows}, f, ensure_ascii=False, indent=2)

    print("\n### finite burst（c本を一括投入し全終了まで待つ makespan throughput。定常容量ではない）\n")
    print("| 並列数 | burst合計 tok/s | 1本あたり R_out | TTFT p50 | TTFT p90 | 完了時間 p50 | スケール効率 |")
    print("|---|---|---|---|---|---|---|")
    base_agg = None
    for conc in levels:
        rs = [r for r in rows if r["concurrency"] == conc]
        aggs = [r["agg_tps"] for r in rs if r["agg_tps"] is not None]
        agg = statistics.mean(aggs) if aggs else None
        if conc == 1:
            base_agg = agg
        per_l = [r["per_stream_decode_p50"] for r in rs if r["per_stream_decode_p50"]]
        t50_l = [r["ttft_p50"] for r in rs if r["ttft_p50"]]
        t90_l = [r["ttft_p90"] for r in rs if r["ttft_p90"]]
        lat_l = [r["latency_p50"] for r in rs if r["latency_p50"]]
        per = statistics.mean(per_l) if per_l else None
        t50 = statistics.mean(t50_l) if t50_l else None
        t90 = statistics.mean(t90_l) if t90_l else None
        lat = statistics.mean(lat_l) if lat_l else None
        eff = (agg / (base_agg * conc)) if (base_agg and agg) else None
        def f(v, d=1, suf=""):
            return "-" if v is None else f"{v:.{d}f}{suf}"
        print(f"| {conc} | {f(agg, 0)} | {f(per)} | {f(t50, 2, 's')} | {f(t90, 2, 's')} | "
              f"{f(lat, 1, 's')} | {(f'{eff * 100:.0f}%') if eff else '-'} |")
    print(f"\nsaved -> {out_dir}")


if __name__ == "__main__":
    main()
