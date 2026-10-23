"""S3: 定常状態の処理能力と SLO goodput（specs/protocol.md §1 S3）。

S1/S2 との違い:
- S1: 1本ずつ、前の完了後
- S2: c本を一括投入して全終了まで待つ（finite burst。makespan throughput）
- S3: **closed-loop**（c本を維持し、1本終わるたびに次を投入）を一定時間継続。
      makespan ではなく定常レートを測る。goodput は SLO を満たした仕事のレート。

使い方:
  python3 steady.py --model X --concurrency 1,2,4,8 --duration 60
  python3 steady.py --model X --mode open --rate 1,2,4 --duration 60

SLO は既定で TTFT <= 1.0s かつ 完了 <= 6.0s。--slo-ttft / --slo-latency で変更。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import threading
import time
from datetime import datetime, timezone

from bench import DEFAULT_BASE, chat_stream, fmt

PROMPT = (
    "次の文章を読んで、内容を日本語で3行に要約してください。\n\n"
    "当社は2024年度からクラウド基盤への移行を進めており、現在は基幹系の7割が移行済みです。"
    "移行に伴い運用コストは前年比12%削減され、障害対応時間も平均40分から18分に短縮されました。"
    "一方で、移行に不慣れな現場からの問い合わせが増えており、教育コストが課題となっています。"
    "2025年度は残り3割の移行と、現場向けの研修プログラムの整備を同時に進める計画です。"
)
MAX_TOKENS = 256


def pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(len(s) * q) - 1))
    return s[i]


def run_closed(base, model, conc, duration, slo_ttft, slo_lat, timeout):
    """c本を維持する closed-loop を duration 秒間継続する。"""
    records: list[dict] = []
    lock = threading.Lock()
    start = time.perf_counter()
    deadline = start + duration

    def worker(idx):
        while time.perf_counter() < deadline:
            try:
                r = chat_stream(base, model, PROMPT, MAX_TOKENS, 0.0, timeout=timeout)
                rec = {
                    "worker": idx,
                    "t_start": round(time.perf_counter() - start, 4),
                    "state": r["state"],
                    "ttft": r["ttft_any"],
                    "latency": r["total"],
                    "completion_tokens": r["completion_tokens"],
                    "R_out": r["R_out"],
                    "finish_reason": r["finish_reason"],
                }
            except Exception as e:  # noqa: BLE001
                rec = {"worker": idx, "state": "error", "error": str(e)[:200],
                       "ttft": None, "latency": None, "completion_tokens": None, "R_out": None}
            with lock:
                records.append(rec)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(conc)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wall = time.perf_counter() - start
    return aggregate(records, wall, conc, "closed", slo_ttft, slo_lat)


def run_open(base, model, rate, duration, slo_ttft, slo_lat, timeout):
    """ポアソン到着（平均 rate req/s）を duration 秒間投入する。"""
    records: list[dict] = []
    lock = threading.Lock()
    start = time.perf_counter()
    deadline = start + duration
    threads = []
    rng = __import__("random").Random(12345)
    while True:
        now = time.perf_counter()
        if now >= deadline:
            break
        t = threading.Thread(
            target=lambda: _one(base, model, start, records, lock, timeout), daemon=True)
        t.start()
        threads.append(t)
        wait = rng.expovariate(rate)
        if time.perf_counter() + wait > deadline:
            break
        time.sleep(wait)
    for t in threads:
        t.join()
    wall = time.perf_counter() - start
    return aggregate(records, wall, None, "open", slo_ttft, slo_lat)


def _one(base, model, start, records, lock, timeout):
    try:
        r = chat_stream(base, model, PROMPT, MAX_TOKENS, 0.0, timeout=timeout)
        rec = {"t_start": round(time.perf_counter() - start, 4), "state": r["state"],
               "ttft": r["ttft_any"], "latency": r["total"],
               "completion_tokens": r["completion_tokens"], "R_out": r["R_out"],
               "finish_reason": r["finish_reason"]}
    except Exception as e:  # noqa: BLE001
        rec = {"state": "error", "error": str(e)[:200], "ttft": None, "latency": None,
               "completion_tokens": None, "R_out": None}
    with lock:
        records.append(rec)


def aggregate(records, wall, conc, mode, slo_ttft, slo_lat):
    ok = [r for r in records if r["state"] == "ok"]
    ttfts = [r["ttft"] for r in ok if r["ttft"] is not None]
    lats = [r["latency"] for r in ok if r["latency"] is not None]
    tokens = sum(r["completion_tokens"] or 0 for r in ok)
    slo_ok = [r for r in ok if (r["ttft"] or 1e9) <= slo_ttft and (r["latency"] or 1e9) <= slo_lat]
    return {
        "mode": mode,
        "concurrency": conc,
        "wall_s": round(wall, 2),
        "requests": len(records),
        "ok": len(ok),
        "errors": len(records) - len(ok),
        "rps": round(len(records) / wall, 2),
        "aggregate_tok_per_s": round(tokens / wall, 1),
        "ttft_p50": round(pct(ttfts, 0.5), 3) if ttfts else None,
        "ttft_p90": round(pct(ttfts, 0.9), 3) if ttfts else None,
        "latency_p50": round(pct(lats, 0.5), 3) if lats else None,
        "latency_p90": round(pct(lats, 0.9), 3) if lats else None,
        "mean_R_out": round(statistics.mean([r["R_out"] for r in ok if r["R_out"]]), 1)
        if any(r["R_out"] for r in ok) else None,
        "slo": {"ttft_max_s": slo_ttft, "latency_max_s": slo_lat},
        "slo_pass": len(slo_ok),
        "slo_pass_rate": round(len(slo_ok) / len(ok), 3) if ok else None,
        "goodput_rps": round(len(slo_ok) / wall, 2),
        "records": records,
    }


def main():
    ap = argparse.ArgumentParser(description="S3 定常スループット / SLO goodput")
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--model", required=True)
    ap.add_argument("--mode", default="closed", choices=["closed", "open"])
    ap.add_argument("--concurrency", default="1,2,4,8")
    ap.add_argument("--rate", default="1,2,4")
    ap.add_argument("--duration", type=float, default=60)
    ap.add_argument("--slo-ttft", type=float, default=1.0)
    ap.add_argument("--slo-latency", type=float, default=6.0)
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--out", default=None)
    ap.add_argument("--sut-meta", action="append", default=[], metavar="K=V")
    args = ap.parse_args()

    out_dir = args.out or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "results", "steady-" + datetime.now().strftime("%Y%m%dT%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)

    if args.mode == "closed":
        levels = [int(x) for x in args.concurrency.split(",")]
    else:
        levels = [float(x) for x in args.rate.split(",")]

    rows = []
    for lv in levels:
        print(f"  {args.mode} {lv} ({args.duration:.0f}s)...", flush=True)
        if args.mode == "closed":
            row = run_closed(args.base_url, args.model, lv, args.duration,
                             args.slo_ttft, args.slo_latency, args.timeout)
        else:
            row = run_open(args.base_url, args.model, lv, args.duration,
                           args.slo_ttft, args.slo_latency, args.timeout)
        rows.append(row)
        print(f"    rps={row['rps']} agg={row['aggregate_tok_per_s']} tok/s "
              f"ttft_p50={row['ttft_p50']}s lat_p50={row['latency_p50']}s "
              f"SLO通過={row['slo_pass_rate']} goodput={row['goodput_rps']} rps", flush=True)

    with open(os.path.join(out_dir, "steady.json"), "w", encoding="utf-8") as f:
        json.dump({"model": args.model, "mode": args.mode, "rows": rows}, f,
                  ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "requests.jsonl"), "w", encoding="utf-8") as f:
        for row in rows:
            for rec in row["records"]:
                f.write(json.dumps({"level": row["concurrency"] or row["mode"], **rec},
                                   ensure_ascii=False) + "\n")

    print("\n### S3 定常（closed-loop = 補充あり。SLO: TTFT<={:.1f}s かつ 完了<={:.1f}s）\n"
          .format(args.slo_ttft, args.slo_latency))
    print("| 水準 | rps | 合計 tok/s | TTFT p50 | TTFT p90 | 完了 p50 | 完了 p90 | SLO通過率 | goodput rps |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['concurrency'] or r['mode']} | {r['rps']} | {fmt(r['aggregate_tok_per_s'], 0)} | "
              f"{fmt(r['ttft_p50'], 2)}s | {fmt(r['ttft_p90'], 2)}s | {fmt(r['latency_p50'], 2)}s | "
              f"{fmt(r['latency_p90'], 2)}s | {fmt((r['slo_pass_rate'] or 0) * 100, 0)}% | {r['goodput_rps']} |")
    print(f"\nsaved -> {out_dir}")


if __name__ == "__main__":
    main()
