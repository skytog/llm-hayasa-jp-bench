# 日本語 LLM ベンチマーク

実行: 2026-10-24T21:24:02+09:00

## GLM-5.3-Flash-Uncensored-TensorFold

| 指標 | 値 |
|---|---|
| Prefill | 1,921 tok/s (傾き法, R²=0.997, n=3中央値) |
| 日本語トークン効率 | 0.820 tok/文字（入力。1文字1トークン超は日本語の割高） |
| Decode (client観測) | 81.0 tok/s (p50 inter-chunk gap 11.3 ms, n=3中央値) |
| Task Time | 38.6 s (全試行平均, 54回) |
| Success Rate | 78% (42/54) / 正常応答内 78% |
| Task 完全合格 | 78% (95%CI 55–91%) |
| TTP (観測時間/成功) | 49.7 s |

- 成功率の Wilson 95%CI: 65–87%（naive iid。反復はクラスタ内反復なので過小評価）
- 日本語出力速度（本文の文字/s、reasoning込みの実効）: 25.1 文字/s
- 日本語生成速度（本文区間のみ）: 354.6 文字/s
- reasoning 行重複率（平均）: 0.23（高いほど反復・degeneration）
- 状態: budget_exhausted=12, pass=42
- コールドスタートTTFT（初回モデルロード込み）: 8.1 s
- 固定オーバーヘッド（切片）: 0.411 s
- ITL p90: 18.2 ms（ばらつきの目安）

| タスク | 合格 | 成功率 | 平均時間 |
|---|---|---|---|
| code_count_words | 3/3 | 100% | 7.6 s |
| code_yen_to_int | 3/3 | 100% | 46.8 s |
| code_lru_cache | 3/3 | 100% | 8.9 s |
| extract_employee_json | 3/3 | 100% | 3.0 s |
| numeric_multi_step | 3/3 | 100% | 4.4 s |
| summary_constraint | 3/3 | 100% | 9.7 s |
| classify_sentiment | 3/3 | 100% | 2.3 s |
| long_context_needle | 3/3 | 100% | 2.4 s |
| code_bugfix_merge_intervals | 3/3 | 100% | 9.3 s |
| reconcile_invoices | 3/3 | 100% | 3.2 s |
| table_extract_filter | 3/3 | 100% | 5.6 s |
| business_days | 3/3 | 100% | 14.3 s |
| prose_explain | 3/3 | 100% | 42.2 s |
| prose_procedure | 3/3 | 100% | 54.7 s |
| code_csv_parser | 0/3 | 0% | 142.2 s |
| code_kanji_number | 0/3 | 0% | 130.4 s |
| prose_email | 0/3 | 0% | 103.9 s |
| prose_longform | 0/3 | 0% | 104.8 s |

失敗の内訳:
- code_csv_parser run0: state=budget_exhausted finish=length err=None
- code_csv_parser run1: state=budget_exhausted finish=length err=None
- code_csv_parser run2: state=budget_exhausted finish=length err=None
- code_kanji_number run0: state=budget_exhausted finish=length err=None
- code_kanji_number run1: state=budget_exhausted finish=length err=None
- code_kanji_number run2: state=budget_exhausted finish=length err=None
- prose_email run0: state=budget_exhausted finish=length err=None
- prose_email run1: state=budget_exhausted finish=length err=None
- prose_email run2: state=budget_exhausted finish=length err=None
- prose_longform run0: state=budget_exhausted finish=length err=None
- prose_longform run1: state=budget_exhausted finish=length err=None
- prose_longform run2: state=budget_exhausted finish=length err=None
