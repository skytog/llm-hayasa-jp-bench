# 日本語 LLM ベンチマーク

実行: 2026-10-24T17:44:34+09:00

## deepseek/deepseek-v4.1-flash-1017

| 指標 | 値 |
|---|---|
| Prefill | - tok/s (傾き法, R²=0.769, n=3中央値) |
| 日本語トークン効率 | 0.756 tok/文字（入力。1文字1トークン超は日本語の割高） |
| Decode (client観測) | 180.1 tok/s / 実効 29.2 文字/s・本文のみ 263.0 文字/s（prose、0.18 文字/token） (p50 inter-chunk gap 241.9 ms, n=3中央値) |
| Task Time | 18.9 s (全試行平均, 12回) |
| Success Rate | 100% (12/12) / 正常応答内 100% |
| Task 完全合格 | 100% (95%CI 51–100%) |
| TTP (観測時間/成功) | 18.9 s |

- 成功率の Wilson 95%CI: 76–100%（naive iid。反復はクラスタ内反復なので過小評価）
- 日本語出力速度（本文の文字/s、reasoning込みの実効）: 50.0 文字/s
- 日本語生成速度（本文区間のみ）: 622.6 文字/s
- reasoning 行重複率（平均）: 0.21（高いほど反復・degeneration）
- 状態: pass=12
- 固定オーバーヘッド（切片）: 0.593 s
- ITL p90: 383.1 ms（ばらつきの目安）

| タスク | 合格 | 成功率 | 平均時間 |
|---|---|---|---|
| prose_explain | 3/3 | 100% | 16.5 s |
| prose_procedure | 3/3 | 100% | 5.7 s |
| prose_email | 3/3 | 100% | 17.9 s |
| prose_longform | 3/3 | 100% | 35.3 s |
