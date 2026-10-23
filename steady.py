"""S3: 定常状態の処理能力と SLO goodput（specs/protocol.md §1 S3）。

S1/S2 との違い:
- S1: 1本ずつ、前の完了後
- S2: c本を一括投入して全終了まで待つ（finite burst。makespan throughput）
- S3: 一定時間の**測定窓**を取る。
  - closed-loop: c本を維持し、1本終わるたびに次を投入
  - open-loop: 平均 rate req/s のポアソン到着。in-flight は --max-inflight で制限

集計の定義:
- 測定窓 = duration 秒。**窓内に完了したリクエストだけ**を throughput の分子にする
- achieved_rps = 窓内完了数 / duration
- aggregate_tok_per_s = 窓内完了の completion_tokens 合計 / duration
- latency = **到着から完了まで**（セマフォ待ちを含む end-to-end）
- goodput_rps = SLO（TTFT <= slo_ttft かつ latency <= slo_latency）を満たした窓内完了 / duration
- 窓外に完了したリクエスト（drain）は件数のみ別記録する

使い方:
  python3 steady.py --model X --concurrency 1,2,4,8 --duration 60
  python3 steady.py --model X --mode open --rate 0.5,1,2 --duration 60 --max-inflight 10

SLO は既定で TTFT <= 1.0s かつ end-to-end <= 6.0s。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import threading
import time
from datetime import datetime, timezone

from bench import DEFAULT_BASE, chat_stream, fmt

PROMPT = "1から200までの整数をカンマ区切りで1行に出力してください。"
MAX_TOKENS = 1024


def pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(len(s) * q) - 1))
    return s[i]


def _request(base, model, timeout, sem, start, deadline):
    """1リクエスト。到着時刻から完了までを end-to-end で記録する。"""
    arrived = time.perf_counter()
    waited = 0.0
    if sem is not None:
        sem.acquire()
        waited = time.perf_counter() - arrived
    try:
        r = chat_stream(base, model, PROMPT, MAX_TOKENS, 0.0, timeout=timeout)
        done = time.perf_counter()
        return {
            "arrived_s": round(arrived - start, 4),
            "wait_s": round(waited, 4),
            "state": r["state"],
            "ttft": r["ttft_any"],
            "latency": round(done - arrived, 4),
            "completed_by_deadline": done <= deadline,
            "completion_tokens": r["completion_tokens"],
            "R_out": r["R_out"],
            "finish_reason": r["finish_reason"],
            "error": r.get("error"),
        }
    except Exception as e:  # noqa: BLE001
        done = time.perf_counter()
        return {"arrived_s": round(arrived - start, 4), "wait_s": round(waited, 4),
                "state": "error", "error": str(e)[:200], "ttft": None,
                "latency": round(done - arrived, 4), "completed_by_deadline": done <= deadline,
                "completion_tokens": None, "R_out": None}
    finally:
        if sem is not None:
            sem.release()


def run_closed(base, model, conc, duration, slo_ttft, slo_lat, timeout):
    """c本を維持する closed-loop を duration 秒間継続する。"""
    records: list[dict] = []
    lock = threading.Lock()
    start = time.perf_counter()
    deadline = start + duration

    def worker():
        while time.perf_counter() < deadline:
            rec = _request(base, model, timeout, None, start, deadline)
            with lock:
                records.append(rec)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(conc)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return aggregate(records, duration, conc, "closed", slo_ttft, slo_lat)


def run_open(base, model, rate, duration, slo_ttft, slo_lat, timeout, max_inflight=10):
    """ポアソン到着（平均 rate req/s）を duration 秒間投入する。

    in-flight を max_inflight に制限する。上限に達したリクエストは待たされ、
    その待ち時間も end-to-end latency に含まれる。
    """
    records: list[dict] = []
    lock = threading.Lock()
    start = time.perf_counter()
    deadline = start + duration
    sem = threading.Semaphore(max_inflight)
    threads = []
    rng = random.Random(12345)

    def one():
        rec = _request(base, model, timeout, sem, start, deadline)
        with lock:
            records.append(rec)

    while True:
        now = time.perf_counter()
        if now >= deadline:
            break
        t = threading.Thread(target=one, daemon=True)
        t.start()
        threads.append(t)
        wait = rng.expovariate(rate)
        if time.perf_counter() + wait > deadline:
            break
        time.sleep(wait)
    for t in threads:
        t.join()
    row = aggregate(records, duration, rate, "open", slo_ttft, slo_lat)
    row["offered_rps"] = rate
    return row


def aggregate(records, duration, level, mode, slo_ttft, slo_lat):
    ok = [r for r in records if r["state"] == "ok"]
    inw = [r for r in ok if r.get("completed_by_deadline")]
    ttfts = [r["ttft"] for r in inw if r["ttft"] is not None]
    lats = [r["latency"] for r in inw if r["latency"] is not None]
    waits = [r["wait_s"] for r in inw if r.get("wait_s") is not None]
    tokens = sum(r["completion_tokens"] or 0 for r in inw)
    slo_ok = [r for r in inw if (r["ttft"] or 1e9) <= slo_ttft and (r["latency"] or 1e9) <= slo_lat]
    return {
        "mode": mode,
        "level": level,
        "concurrency": level if mode == "closed" else None,
        "duration_s": duration,
        "requests_sent": len(records),
        "ok": len(ok),
        "errors": len(records) - len(ok),
        "completed_in_window": len(inw),
        "drain_outside_window": len(ok) - len(inw),
        "achieved_rps": round(len(inw) / duration, 3),
        "aggregate_tok_per_s": round(tokens / duration, 1),
        "ttft_p50": round(pct(ttfts, 0.5), 3) if ttfts else None,
        "ttft_p90": round(pct(ttfts, 0.9), 3) if ttfts else None,
        "latency_p50": round(pct(lats, 0.5), 3) if lats else None,
        "latency_p90": round(pct(lats, 0.9), 3) if lats else None,
        "queue_wait_p50": round(pct(waits, 0.5), 3) if waits else None,
        "mean_R_out": round(statistics.mean([r["R_out"] for r in inw if r["R_out"]]), 1)
        if any(r["R_out"] for r in inw) else None,
        "slo": {"ttft_max_s": slo_ttft, "latency_max_s": slo_lat},
        "slo_pass": len(slo_ok),
        "slo_pass_rate": round(len(slo_ok) / len(inw), 3) if inw else None,
        "goodput_rps": round(len(slo_ok) / duration, 3),
        "records": records,
    }


def main():
    ap = argparse.ArgumentParser(description="S3 定常スループット / SLO goodput")
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--model", required=True)
    ap.add_argument("--mode", default="closed", choices=["closed", "open"])
    ap.add_argument("--concurrency", default="1,2,4,8")
    ap.add_argument("--rate", default="0.5,1,2")
    ap.add_argument("--duration", type=float, default=60)
    ap.add_argument("--slo-ttft", type=float, default=1.0)
    ap.add_argument("--slo-latency", type=float, default=6.0)
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--max-inflight", type=int, default=10)
    ap.add_argument("--gap", type=float, default=20,
                    help="水準間の idle gap 秒（前の水準の余熱・backlog を抜く）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out_dir = args.out or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "results", "steady-" + datetime.now().strftime("%Y%m%dT%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)

    levels = ([int(x) for x in args.concurrency.split(",")] if args.mode == "closed"
              else [float(x) for x in args.rate.split(",")])

    rows = []
    for i, lv in enumerate(levels):
        if i > 0 and args.gap > 0:
            print(f"  idle gap {args.gap:.0f}s...", flush=True)
            time.sleep(args.gap)
            chat_stream(args.base_url, args.model, "1+1は?", 8, 0.0, timeout=120)
        print(f"  {args.mode} {lv} ({args.duration:.0f}s)...", flush=True)
        if args.mode == "closed":
            row = run_closed(args.base_url, args.model, lv, args.duration,
                             args.slo_ttft, args.slo_latency, args.timeout)
        else:
            row = run_open(args.base_url, args.model, lv, args.duration,
                           args.slo_ttft, args.slo_latency, args.timeout, args.max_inflight)
        rows.append(row)
        print(f"    achieved_rps={row['achieved_rps']} agg={row['aggregate_tok_per_s']} tok/s "
              f"ttft_p50={row['ttft_p50']}s lat_p50={row['latency_p50']}s "
              f"wait_p50={row['queue_wait_p50']}s SLO={row['slo_pass_rate']} "
              f"goodput={row['goodput_rps']} err={row['errors']}", flush=True)

    with open(os.path.join(out_dir, "steady.json"), "w", encoding="utf-8") as f:
        json.dump({"model": args.model, "mode": args.mode, "rows": rows}, f,
                  ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "requests.jsonl"), "w", encoding="utf-8") as f:
        for row in rows:
            for rec in row["records"]:
                f.write(json.dumps({"level": row["level"], **rec}, ensure_ascii=False) + "\n")

    print("\n### S3 定常（{}、測定窓 {:.0f}s、SLO: TTFT<={:.1f}s かつ end-to-end<={:.1f}s）\n"
          .format("closed-loop = 補充あり" if args.mode == "closed" else "open-loop = ポアソン到着",
                  args.duration, args.slo_ttft, args.slo_latency))
    print("| 水準 | achieved rps | 合計 tok/s | TTFT p50 | 完了 p50 | 完了 p90 | 待ち p50 | SLO通過率 | goodput rps | err |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lvl = f"{r['offered_rps']}→{r['achieved_rps']}" if r.get("offered_rps") else f"{r['level']}"
        print(f"| {lvl} | {r['achieved_rps']} | {fmt(r['aggregate_tok_per_s'], 0)} | "
              f"{fmt(r['ttft_p50'], 2)}s | {fmt(r['latency_p50'], 2)}s | {fmt(r['latency_p90'], 2)}s | "
              f"{fmt(r['queue_wait_p50'], 2)}s | {fmt((r['slo_pass_rate'] or 0) * 100, 0)}% | "
              f"{r['goodput_rps']} | {r['errors']} |")
    print(f"\nsaved -> {out_dir}")


if __name__ == "__main__":
    main()
