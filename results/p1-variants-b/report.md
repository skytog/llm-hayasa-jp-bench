# 日本語 LLM ベンチマーク

実行: 2026-10-24T22:01:26+09:00

## GLM-5.3-Flash-Uncensored-TensorFold

| 指標 | 値 |
|---|---|
| Prefill | - tok/s (TTFT_any 全対回帰, R²=-, -ブロック×-回) |
| 日本語トークン効率 | - tok/文字（入力。1文字1トークン超は日本語の割高） |
| Decode (client観測) | - tok/s (p50 inter-chunk gap - ms, n=-中央値) |
| Task Time | 98.0 s (全試行平均, 4回) |
| Success Rate | 25% (1/4) / 正常応答内 25% |
| Task 完全合格 | 25% (95%CI 5–70%) |
| TTP (観測時間/成功) | 392.1 s |

- 成功率の Wilson 95%CI: 5–70%（naive iid。反復はクラスタ内反復なので過小評価）
- 日本語出力速度（本文の文字/s、reasoning込みの実効）: 7.2 文字/s
- 日本語生成速度（本文区間のみ）: 176.1 文字/s
- reasoning 行重複率（平均）: 0.27（高いほど反復・degeneration）
- 状態: budget_exhausted=2, pass=1, wrong_answer=1

| タスク | 合格 | 成功率 | 平均時間 |
|---|---|---|---|
| prose_explain_2 | 1/1 | 100% | 73.3 s |
| prose_procedure_2 | 0/1 | 0% | 101.0 s |
| prose_email_2 | 0/1 | 0% | 109.1 s |
| prose_longform_2 | 0/1 | 0% | 108.8 s |

失敗の内訳:
- prose_procedure_2 run0: state=budget_exhausted finish=length err=None
- prose_email_2 run0: state=budget_exhausted finish=length err=None
- prose_longform_2 run0: 693文字（1000-1400が必要）; 段落4（5が必要）; 3文未満の段落=[4]
