# 日本語 LLM ベンチマーク

実行: 2026-10-24T01:04:05+09:00

## deepseek/deepseek-v4.1-flash-1017

| 指標 | 値 |
|---|---|
| Prefill | 28,491 tok/s (傾き法, R²=0.992, n=3中央値) |
| 日本語トークン効率 | 0.741 tok/文字（入力。1文字1トークン超は日本語の割高） |
| Decode (client観測) | 132.1 tok/s (p50 inter-chunk gap 0.0 ms, n=3中央値) |
| Task Time | 46.0 s (全試行平均, 6回) |
| Success Rate | 33% (2/6) / 正常応答内 33% |
| Task 完全合格 | 0% (95%CI 0–66%) |
| TTP (観測時間/成功) | 138.1 s |

- 成功率の Wilson 95%CI: 10–70%（naive iid。反復はクラスタ内反復なので過小評価）
- reasoning 行重複率（平均）: 0.37（高いほど反復・degeneration）
- 状態: budget_exhausted=4, pass=2
- 固定オーバーヘッド（切片）: 0.410 s
- ITL p90: 115.9 ms（ばらつきの目安）

| タスク | 合格 | 成功率 | 平均時間 |
|---|---|---|---|
| code_csv_parser | 1/3 | 33% | 26.8 s |
| code_kanji_number | 1/3 | 33% | 65.2 s |

失敗の内訳:
- code_csv_parser run0: state=budget_exhausted finish=length err=None
- code_csv_parser run1: state=budget_exhausted finish=length err=None
- code_kanji_number run0: state=budget_exhausted finish=length err=None
- code_kanji_number run1: state=budget_exhausted finish=length err=None
