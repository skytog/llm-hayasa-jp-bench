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
        return chat_stream(base, model, PROMPT, MAX_TOKENS, temperature, 900)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:160], "completion_tokens": 0, "ttft": None,
                "total": 0.0, "decode_tps": None, "itls": []}


def run_level(base, model, conc, temperature):
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as pool:
        futures = [pool.submit(_safe_chat, base, model, temperature) for _ in range(conc)]
        results = [f.result() for f in futures]
    wall = time.perf_counter() - t0

    ok = [r for r in results if r["completion_tokens"]]
    errors = [r for r in results if not r["completion_tokens"]]
    total_tokens = sum(r["completion_tokens"] for r in ok)
    ttfts = [r["ttft"] for r in ok if r["ttft"]]
    decodes = [r["decode_tps"] for r in ok if r["decode_tps"]]
    totals = [r["total"] for r in ok]
    return {
        "concurrency": conc,
        "requests_ok": len(ok),
        "errors": len(errors),
        "error_sample": errors[0]["error"] if errors else None,
        "wall_s": round(wall, 3),
        "total_completion_tokens": total_tokens,
        "agg_tps": round(total_tokens / wall, 1) if wall > 0 else None,
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

    print("\n| 並列数 | 合計 tok/s | 1本あたり decode | TTFT p50 | TTFT p90 | 完了時間 p50 | スケール効率 |")
    print("|---|---|---|---|---|---|---|")
    base_agg = None
    for conc in levels:
        rs = [r for r in rows if r["concurrency"] == conc]
        agg = statistics.mean([r["agg_tps"] for r in rs])
        if conc == levels[0]:
            base_agg = agg
        per = statistics.mean([r["per_stream_decode_p50"] for r in rs if r["per_stream_decode_p50"]])
        t50 = statistics.mean([r["ttft_p50"] for r in rs if r["ttft_p50"]])
        t90 = statistics.mean([r["ttft_p90"] for r in rs if r["ttft_p90"]])
        lat = statistics.mean([r["latency_p50"] for r in rs if r["latency_p50"]])
        eff = (agg / (base_agg * conc)) if base_agg else None
        print(f"| {conc} | {agg:.0f} | {per:.1f} | {t50:.2f}s | {t90:.2f}s | {lat:.1f}s | "
              f"{eff * 100:.0f}% |" if eff else f"| {conc} | {agg:.0f} | {per:.1f} | {t50:.2f}s | {t90:.2f}s | {lat:.1f}s | - |")
    print(f"\nsaved -> {out_dir}")


if __name__ == "__main__":
    main()
