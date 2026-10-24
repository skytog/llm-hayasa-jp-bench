# 日本語 LLM ベンチマーク

実行: 2026-10-24T20:37:55+09:00

## DeepSeek-V4.1-Flash-1017-BLACKFOLD-TP2

| 指標 | 値 |
|---|---|
| Prefill | 2,276 tok/s (傾き法, R²=0.972, n=3中央値) |
| 日本語トークン効率 | 0.764 tok/文字（入力。1文字1トークン超は日本語の割高） |
| Decode (client観測) | 93.7 tok/s / 実効 16.9 文字/s・本文のみ 139.3 文字/s（prose、0.18 文字/token） (p50 inter-chunk gap 39.1 ms, n=3中央値) |
| Task Time | 25.1 s (全試行平均, 54回) |
| Success Rate | 83% (45/54) / 正常応答内 83% |
| Task 完全合格 | 83% (95%CI 61–94%) |
| TTP (観測時間/成功) | 30.1 s |

- 成功率の Wilson 95%CI: 71–91%（naive iid。反復はクラスタ内反復なので過小評価）
- 日本語出力速度（本文の文字/s、reasoning込みの実効）: 31.1 文字/s
- 日本語生成速度（本文区間のみ）: 229.5 文字/s
- reasoning 行重複率（平均）: 0.25（高いほど反復・degeneration）
- 状態: budget_exhausted=6, pass=45, wrong_answer=3
- コールドスタートTTFT（初回モデルロード込み）: 0.2 s
- 固定オーバーヘッド（切片）: 0.493 s
- ITL p90: 44.4 ms（ばらつきの目安）

| タスク | 合格 | 成功率 | 平均時間 |
|---|---|---|---|
| code_count_words | 3/3 | 100% | 20.6 s |
| code_yen_to_int | 3/3 | 100% | 14.2 s |
| code_lru_cache | 3/3 | 100% | 5.8 s |
| extract_employee_json | 3/3 | 100% | 1.4 s |
| numeric_multi_step | 3/3 | 100% | 6.7 s |
| summary_constraint | 3/3 | 100% | 7.9 s |
| classify_sentiment | 3/3 | 100% | 1.4 s |
| long_context_needle | 3/3 | 100% | 2.4 s |
| code_bugfix_merge_intervals | 3/3 | 100% | 4.9 s |
| reconcile_invoices | 3/3 | 100% | 1.8 s |
| table_extract_filter | 3/3 | 100% | 2.2 s |
| business_days | 3/3 | 100% | 6.9 s |
| prose_explain | 3/3 | 100% | 32.8 s |
| prose_procedure | 3/3 | 100% | 21.3 s |
| prose_email | 3/3 | 100% | 40.0 s |
| code_csv_parser | 0/3 | 0% | 90.7 s |
| code_kanji_number | 0/3 | 0% | 102.8 s |
| prose_longform | 0/3 | 0% | 88.3 s |

失敗の内訳:
- code_csv_parser run0: state=budget_exhausted finish=length err=None
- code_csv_parser run1: state=budget_exhausted finish=length err=None
- code_csv_parser run2: state=budget_exhausted finish=length err=None
- code_kanji_number run0: state=budget_exhausted finish=length err=None
- code_kanji_number run1: state=budget_exhausted finish=length err=None
- code_kanji_number run2: state=budget_exhausted finish=length err=None
- prose_longform run0: 748文字（1000-1400が必要）; 段落4（5が必要）; 3文未満の段落=[4]
- prose_longform run1: 748文字（1000-1400が必要）; 段落4（5が必要）; 3文未満の段落=[4]
- prose_longform run2: 748文字（1000-1400が必要）; 段落4（5が必要）; 3文未満の段落=[4]
