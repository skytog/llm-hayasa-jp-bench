#!/usr/bin/env python3
"""results.json を読んでモデル横断の比較表を出す.

使い方:
  python3 compare.py                      # 最新の results/*/results.json
  python3 compare.py results/20261023T125239/results.json
"""
import glob
import json
import os
import sys


def fmt(v, d=1):
    return "-" if v is None else f"{v:,.{d}f}"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        cands = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                              "results", "*", "results.json")))
        if not cands:
            sys.exit("no results.json found")
        path = cands[-1]
    data = json.load(open(path, encoding="utf-8"))

    print(f"# モデル比較 ({os.path.basename(os.path.dirname(path))})\n")
    print("| モデル | Prefill tok/s | Decode tok/s | Task平均 s | 成功率 | Time-to-Pass s |")
    print("|---|---|---|---|---|---|")
    rows = []
    for r in data:
        s = r["speed"] or {}
        sm = r["summary"]
        rows.append((r["model"], s.get("prefill_tps"), s.get("decode_tps"),
                     sm.get("mean_task_time_s"), sm.get("success_rate"),
                     sm.get("time_to_pass_s")))
    rows.sort(key=lambda x: (x[5] is None, x[5] if x[5] else 1e9))
    for m, pre, dec, t, sr, ttp in rows:
        print(f"| {m} | {fmt(pre, 0)} | {fmt(dec, 1)} | {fmt(t, 1)} | "
              f"{(sr * 100):.0f}% | {fmt(ttp, 1)} |")

    print("\n## タスク別 成功率\n")
    tasks = sorted({t["task"] for r in data for t in r["tasks"]})
    print("| タスク | " + " | ".join(r["model"] for r in data) + " |")
    print("|---" * (len(data) + 1) + "|")
    for tid in tasks:
        cells = []
        for r in data:
            rs = [t for t in r["tasks"] if t["task"] == tid]
            p = sum(1 for t in rs if t["passed"])
            cells.append(f"{p}/{len(rs)}")
        print(f"| {tid} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
