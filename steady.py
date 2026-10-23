"""S3: 定常状態の処理能力と SLO goodput（specs/protocol.md §1 S3）。補助指標。

S1/S2 との違い:
- S1: 1本ずつ、前の完了後
- S2: c本を一括投入して全終了まで待つ（finite burst。makespan throughput）
- S3: 一定時間の**測定窓**を取る。
  - closed-loop: c本を維持し、1本終わるたびに次を投入
  - open-loop: 平均 rate req/s のポアソン到着。in-flight は --max-inflight で制限

集計の定義（sol レビュー v2 の指摘を反映）:
- 測定窓 = duration 秒。**窓内に完了したリクエストだけ**を throughput の分子にする
- latency = **到着から完了まで**（クライアント待ちを含む end-to-end）
- TTFT は2種類: `ttft_send`（送信後）と `ttft_arrival`（到着から。待ち時間込み）
- SLO 判定は **ttft_arrival** と end-to-end latency で行う
- goodput は **finish_reason == 'stop'**（自然終了）の完了だけを数える
- usage 不明のリクエストはトークン集計から外し、coverage を併記する
- 分母（sent / ok / errors / in_window / stop / length）を必ず表示する

使い方:
  python3 steady.py --model X --concurrency 1,2,4,8 --duration 60
  python3 steady.py --model X --mode open --rate 0.1,0.2,0.3 --duration 60 --max-inflight 10
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


def _request(base, model, timeout, start, deadline, scheduled_s):
    """1リクエスト。予定到着・実到着・送信後TTFT・到着基準TTFT を記録する。"""
    arrived = time.perf_counter()
    try:
        r = chat_stream(base, model, PROMPT, MAX_TOKENS, 0.0, timeout=timeout)
        done = time.perf_counter()
        return {
            "scheduled_s": round(scheduled_s, 4),
            "arrived_s": round(arrived - start, 4),
            "arrival_delay_s": round(arrived - start - scheduled_s, 4),
            "state": r["state"],
            "finish_reason": r["finish_reason"],
            "ttft_send": r["ttft_any"],
            "ttft_arrival": ((arrived - start) + r["ttft_any"]) if r["ttft_any"] is not None else None,
            "latency": round(done - arrived, 4),
            "completed_by_deadline": done <= deadline,
            "completion_tokens": r["completion_tokens"],
            "usage_available": r["usage_available"],
            "R_out": r["R_out"],
            "error": r.get("error"),
        }
    except Exception as e:  # noqa: BLE001
        done = time.perf_counter()
        return {"scheduled_s": round(scheduled_s, 4), "arrived_s": round(arrived - start, 4),
                "arrival_delay_s": round(arrived - start - scheduled_s, 4),
                "state": "error", "error": str(e)[:200], "finish_reason": None,
                "ttft_send": None, "ttft_arrival": None,
                "latency": round(done - arrived, 4),
                "completed_by_deadline": done <= deadline,
                "completion_tokens": None, "usage_available": False, "R_out": None}


def run_closed(base, model, conc, duration, slo_ttft, slo_lat, timeout):
    records: list[dict] = []
    lock = threading.Lock()
    start = time.perf_counter()
    deadline = start + duration

    def worker():
        while time.perf_counter() < deadline:
            scheduled = time.perf_counter() - start
            rec = _request(base, model, timeout, start, deadline, scheduled)
            with lock:
                records.append(rec)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(conc)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return aggregate(records, duration, conc, "closed", slo_ttft, slo_lat)


def run_open(base, model, rate, duration, slo_ttft, slo_lat, timeout, max_inflight=10):
    """ポアソン到着。in-flight は max_inflight。満杯なら到着自体が待たされる。"""
    records: list[dict] = []
    lock = threading.Lock()
    start = time.perf_counter()
    deadline = start + duration
    sem = threading.Semaphore(max_inflight)
    threads = []
    rng = random.Random(12345)

    def one(scheduled):
        rec = _request(base, model, timeout, start, deadline, scheduled)
        with lock:
            records.append(rec)
        sem.release()

    while True:
        now = time.perf_counter()
        if now >= deadline:
            break
        scheduled = now - start
        sem.acquire()  # 満杯なら待つ（arrival_delay として記録される）
        t = threading.Thread(target=one, args=(scheduled,), daemon=True)
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
    stopped = [r for r in inw if r.get("finish_reason") == "stop"]
    with_usage = [r for r in inw if r.get("usage_available") and r.get("completion_tokens")]
    tokens = sum(r["completion_tokens"] for r in with_usage)
    ttft_arr = [r["ttft_arrival"] for r in inw if r["ttft_arrival"] is not None]
    ttft_snd = [r["ttft_send"] for r in inw if r["ttft_send"] is not None]
    lats = [r["latency"] for r in inw if r["latency"] is not None]
    delays = [r["arrival_delay_s"] for r in inw if r.get("arrival_delay_s") is not None]
    # SLO は 到着基準 TTFT と end-to-end latency。自然終了(stop)のみを合格候補にする
    slo_ok = [r for r in stopped
              if (r["ttft_arrival"] or 1e9) <= slo_ttft and (r["latency"] or 1e9) <= slo_lat]
    coverage = round(len(with_usage) / len(inw), 3) if inw else None
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
        "stop": len(stopped),
        "length": sum(1 for r in inw if r.get("finish_reason") == "length"),
        "achieved_rps": round(len(inw) / duration, 3),
        "aggregate_tok_per_s": round(tokens / duration, 1),
        "token_coverage": coverage,
        "ttft_send_p50": round(pct(ttft_snd, 0.5), 3) if ttft_snd else None,
        "ttft_send_p90": round(pct(ttft_snd, 0.9), 3) if ttft_snd else None,
        "ttft_arrival_p50": round(pct(ttft_arr, 0.5), 3) if ttft_arr else None,
        "ttft_arrival_p90": round(pct(ttft_arr, 0.9), 3) if ttft_arr else None,
        "latency_p50": round(pct(lats, 0.5), 3) if lats else None,
        "latency_p90": round(pct(lats, 0.9), 3) if lats else None,
        "arrival_delay_p50": round(pct(delays, 0.5), 3) if delays else None,
        "mean_R_out": round(statistics.mean([r["R_out"] for r in inw if r["R_out"]]), 1)
        if any(r["R_out"] for r in inw) else None,
        "slo": {"ttft_max_s": slo_ttft, "latency_max_s": slo_lat, "basis": "arrival + end-to-end"},
        "slo_pass": len(slo_ok),
        "slo_denominator": len(stopped),
        "slo_pass_rate": round(len(slo_ok) / len(stopped), 3) if stopped else None,
        "goodput_rps": round(len(slo_ok) / duration, 3),
        "records": records,
    }


def main():
    ap = argparse.ArgumentParser(description="S3 定常スループット / SLO goodput（補助指標）")
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--model", required=True)
    ap.add_argument("--mode", default="closed", choices=["closed", "open"])
    ap.add_argument("--concurrency", default="1,2,4,8")
    ap.add_argument("--rate", default="0.1,0.2,0.3")
    ap.add_argument("--duration", type=float, default=60)
    ap.add_argument("--slo-ttft", type=float, default=1.0)
    ap.add_argument("--slo-latency", type=float, default=6.0)
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--max-inflight", type=int, default=10)
    ap.add_argument("--gap", type=float, default=20)
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
        print(f"    sent={row['requests_sent']} ok={row['ok']} err={row['errors']} "
              f"inwin={row['completed_in_window']} stop={row['stop']} length={row['length']} "
              f"rps={row['achieved_rps']} tok/s={row['aggregate_tok_per_s']} "
              f"(coverage={row['token_coverage']}) "
              f"ttft_arrival_p50={row['ttft_arrival_p50']} lat_p50={row['latency_p50']} "
              f"SLO={row['slo_pass']}/{row['slo_denominator']} goodput={row['goodput_rps']}",
              flush=True)

    with open(os.path.join(out_dir, "steady.json"), "w", encoding="utf-8") as f:
        json.dump({"model": args.model, "mode": args.mode, "rows": rows}, f,
                  ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "requests.jsonl"), "w", encoding="utf-8") as f:
        for row in rows:
            for rec in row["records"]:
                f.write(json.dumps({"level": row["level"], **rec}, ensure_ascii=False) + "\n")

    print("\n### S3 補助（{}、窓 {:.0f}s、SLO: 到着基準TTFT<={:.1f}s かつ end-to-end<={:.1f}s、自然終了のみ）\n"
          .format("closed-loop" if args.mode == "closed" else "open-loop",
                  args.duration, args.slo_ttft, args.slo_latency))
    print("| 水準 | sent | ok | err | 窓内 | stop | length | rps | 合計tok/s | coverage | "
          "TTFT到着p50 | 完了p50 | 待ちp50 | SLO k/n | goodput rps |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lvl = f"{r['offered_rps']}→{r['achieved_rps']}" if r.get("offered_rps") else f"{r['level']}"
        print(f"| {lvl} | {r['requests_sent']} | {r['ok']} | {r['errors']} | "
              f"{r['completed_in_window']} | {r['stop']} | {r['length']} | {r['achieved_rps']} | "
              f"{fmt(r['aggregate_tok_per_s'], 0)} | {fmt((r['token_coverage'] or 0) * 100, 0)}% | "
              f"{fmt(r['ttft_arrival_p50'], 2)}s | {fmt(r['latency_p50'], 2)}s | "
              f"{fmt(r['arrival_delay_p50'], 2)}s | {r['slo_pass']}/{r['slo_denominator']} | "
              f"{r['goodput_rps']} |")
    print(f"\nsaved -> {out_dir}")


if __name__ == "__main__":
    main()
