"""2つの results.json を対応のあるタスク集合として比較する（P1: cluster 統計の最小実装）。

- タスク単位の成功（全反復 PASS）を対応とする
- 不一致タスク（A だけ成功 / B だけ成功）で McNemar 正確検定
- タスクをクラスタとしてブートストラップし、成功率差・平均時間差の 95%CI
- 反復の相関を無視しないため、attempt 単位の Wilson CI ではなくタスクを再標本化する

使い方:
  python3 stats.py results/spec-v3-blackfold/results.json results/spec-v3-glm/results.json
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from math import comb


def load(path):
    d = json.load(open(path, encoding="utf-8"))
    return d[0] if isinstance(d, list) else d


def task_table(run):
    """task -> {"passes": n, "runs": m, "times": [...]}"""
    out = {}
    for t in run["tasks"]:
        e = out.setdefault(t["task"], {"passes": 0, "runs": 0, "times": []})
        e["runs"] += 1
        e["passes"] += 1 if t["passed"] else 0
        e["times"].append(t["elapsed"])
    return out


def mcnemar_exact(b, c):
    """b = A だけ成功、c = B だけ成功。両側正確検定。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def cluster_bootstrap(a, b, n_boot=4000, seed=1234):
    """タスクを再標本化して、成功率差と平均時間差の 95%CI。"""
    tasks = sorted(set(a) & set(b))
    rng = random.Random(seed)
    ds, dt = [], []
    for _ in range(n_boot):
        pick = [tasks[rng.randrange(len(tasks))] for _ in tasks]
        sa = sum(1 for t in pick if a[t]["passes"] == a[t]["runs"]) / len(pick)
        sb = sum(1 for t in pick if b[t]["passes"] == b[t]["runs"]) / len(pick)
        ds.append(sb - sa)
        ta = statistics.mean([statistics.mean(a[t]["times"]) for t in pick])
        tb = statistics.mean([statistics.mean(b[t]["times"]) for t in pick])
        dt.append(tb - ta)
    ds.sort(); dt.sort()
    def ci(x):
        return [round(x[int(0.025 * len(x))], 4), round(x[min(len(x) - 1, int(0.975 * len(x)))], 4)]
    return ci(ds), ci(dt)


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: python3 stats.py A/results.json B/results.json")
    ra, rb = load(sys.argv[1]), load(sys.argv[2])
    a, b = task_table(ra), task_table(rb)
    tasks = sorted(set(a) & set(b))
    only_a = [t for t in tasks if a[t]["passes"] == a[t]["runs"] and b[t]["passes"] < b[t]["runs"]]
    only_b = [t for t in tasks if b[t]["passes"] == b[t]["runs"] and a[t]["passes"] < a[t]["runs"]]
    p = mcnemar_exact(len(only_a), len(only_b))
    ds, dt = cluster_bootstrap(a, b)

    print(f"A: {ra['model']}")
    print(f"B: {rb['model']}")
    print(f"共通タスク: {len(tasks)}")
    sa = sum(1 for t in tasks if a[t]["passes"] == a[t]["runs"])
    sb = sum(1 for t in tasks if b[t]["passes"] == b[t]["runs"])
    print(f"タスク完全合格: A {sa}/{len(tasks)}  B {sb}/{len(tasks)}")
    print(f"不一致: A だけ合格 {only_a}")
    print(f"        B だけ合格 {only_b}")
    print(f"McNemar 正確検定 p = {p:.4f}")
    print(f"成功率差 (B-A) の95%CI: {ds[0] * 100:.0f}〜{ds[1] * 100:.0f} ポイント（cluster bootstrap）")
    print(f"平均時間差 (B-A) の95%CI: {dt[0]:.1f}〜{dt[1]:.1f} 秒（cluster bootstrap）")
    print("\n注意: タスク数が少なく、入力も少数。一般化のCIではない（pilot）。")


if __name__ == "__main__":
    main()
