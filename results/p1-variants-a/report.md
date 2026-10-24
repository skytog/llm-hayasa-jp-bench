# 日本語 LLM ベンチマーク

実行: 2026-10-24T21:51:53+09:00

## DeepSeek-V4.1-Flash-1017-BLACKFOLD-TP2

| 指標 | 値 |
|---|---|
| Prefill | - tok/s (TTFT_any 全対回帰, R²=-, -ブロック×-回) |
| 日本語トークン効率 | - tok/文字（入力。1文字1トークン超は日本語の割高） |
| Decode (client観測) | - tok/s (p50 inter-chunk gap - ms, n=-中央値) |
| Task Time | 61.8 s (全試行平均, 4回) |
| Success Rate | 100% (4/4) / 正常応答内 100% |
| Task 完全合格 | 100% (95%CI 51–100%) |
| TTP (観測時間/成功) | 61.8 s |

- 成功率の Wilson 95%CI: 51–100%（naive iid。反復はクラスタ内反復なので過小評価）
- 日本語出力速度（本文の文字/s、reasoning込みの実効）: 9.3 文字/s
- 日本語生成速度（本文区間のみ）: 136.5 文字/s
- reasoning 行重複率（平均）: 0.34（高いほど反復・degeneration）
- 状態: pass=4

| タスク | 合格 | 成功率 | 平均時間 |
|---|---|---|---|
| prose_explain_2 | 1/1 | 100% | 63.2 s |
| prose_procedure_2 | 1/1 | 100% | 65.3 s |
| prose_email_2 | 1/1 | 100% | 35.8 s |
| prose_longform_2 | 1/1 | 100% | 82.8 s |
