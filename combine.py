#!/usr/bin/env python3
"""複数の results.json を結合して総合表を出す（易しいセット＋難しいセット）.

使い方:
  python3 combine.py results/final4/results.json results/hard4/results.json
"""
import json
import statistics
import sys


def fmt(v, d=1):
    return "-" if v is None else f"{v:,.{d}f}"


def main():
    paths = sys.argv[1:] or ["results/final4/results.json", "results/hard4/results.json"]
    merged = {}
    for p in paths:
        for r in json.load(open(p, encoding="utf-8")):
            m = merged.setdefault(r["model"], {"tasks": [], "speed": r["speed"]})
            m["tasks"].extend(r["tasks"])
            if not m["speed"] and r["speed"]:
                m["speed"] = r["speed"]

    rows = []
    for model, m in merged.items():
        tasks = m["tasks"]
        passes = sum(1 for t in tasks if t["passed"])
        times = [t["elapsed"] for t in tasks if t["elapsed"]]
        sr = passes / len(tasks)
        mt = statistics.mean(times)
        rows.append({
            "model": model,
            "prefill": (m["speed"] or {}).get("prefill_tps"),
            "decode": (m["speed"] or {}).get("decode_tps"),
            "mean": mt,
            "success": sr,
            "passes": passes,
            "total": len(tasks),
            "ttp": mt / sr if sr > 0 else None,
        })
    rows.sort(key=lambda x: (x["ttp"] is None, x["ttp"] or 1e9))

    print("| モデル | Prefill tok/s | Decode tok/s | Task平均 s | 成功率 | Time-to-Pass s |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['model']} | {fmt(r['prefill'], 0)} | {fmt(r['decode'], 1)} | "
              f"{fmt(r['mean'], 1)} | {r['passes']}/{r['total']} ({r['success'] * 100:.0f}%) | "
              f"{fmt(r['ttp'], 1)} |")

    print("\n## タスク別 成功率\n")
    tids = sorted({t["task"] for m in merged.values() for t in m["tasks"]})
    order = [r["model"] for r in rows]
    print("| タスク | " + " | ".join(order) + " |")
    print("|---" * (len(order) + 1) + "|")
    for tid in tids:
        cells = []
        for model in order:
            rs = [t for t in merged[model]["tasks"] if t["task"] == tid]
            p = sum(1 for t in rs if t["passed"])
            cells.append(f"{p}/{len(rs)}")
        print(f"| {tid} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
