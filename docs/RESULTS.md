# 測定結果（2026-10-24、2 SUT）

このファイルは **同じハーネス・同じタスク・同じ予算**で測った2つの SUT の結果。
数値は生データ（`results/*/attempts.jsonl`）から再生成できる。指標の定義は `specs/metrics.md`。

**主張できること**: 日本語の固定 workload に対する、クライアント観測の TTFT 傾き・出力レート・タスク終結時間。
**主張できないこと**: 素の推論性能、token 単位の ITL、日本語能力の保証、SUT 間の統計的有意差（各3回・単一マシン・単一経路）。

## SUT

| | A | B |
|---|---|---|
| モデル | DeepSeek-V4.1-Flash-1017-BLACKFOLD-TP2 | GLM-5.3-Flash-Uncensored-TensorFold |
| 量子化 | EXL3 | TensorFold |
| GPU | GB10 ×2、TP2 | GB10 ×2 |
| engine | unknown（llama-swap recipe） | llama-swap（TensorFold worker） |
| 経路 | Tailscale | Tailscale |
| コンテキスト上限 | 1,048,576 | unknown |
| cache | unknown | unknown |
| sampling | temperature 0 | temperature 0 |

同じマシン・同じ経路で、**同時に1モデルだけ**ロードされる構成。A と B は別ランで測り、間にモデル入れ替え（約166秒）が入る。

## 速度3指標

| 指標 | A: BLACKFOLD | B: GLM |
|---|---|---|
| **Prefill**（入力長傾きの逆数） | **2,154 tok/s**（R²=1.000、切片 0.384s、9/9 有効） | **1,886 tok/s**（R²=0.991、切片 0.344s、9/9 有効） |
| **Decode**（R_out、client 観測） | **120.0 tok/s**（gap p50 43.0ms、5.12 tok/chunk） | **81.0 tok/s**（gap p50 11.3ms、1.01 tok/chunk） |
| 日本語トークン効率（入力） | 0.764 tok/文字 | 0.821 tok/文字 |
| **Task 完了時間**（18問×3、全試行平均） | **24.2 s** | **38.7 s** |
| 成功率 | 45/54（83.3%） | 42/54（77.8%） |
| TTP（観測時間/成功） | 29.1 s | 49.8 s |
| コールドスタート TTFT | 0.19 s（resident 済み） | 7.93 s（15回試行で warm 判定） |

Decode の信頼度は SUT で違う:

- **A**: 5.12 tok/chunk（サーバーが複数トークンをまとめて返す）。`R_out` は「見かけの出力レート」で、token 単位の ITL ではない。
- **B**: 1.01 tok/chunk（1トークン1チャンク）。`gap p50 11.3ms` は **ほぼ真の token 間隔**で、81 tok/s と整合する。**B の decode のほうが解釈が素直**。

## 日本語散文の速度

400〜600字の日本語散文を出力させた probe（`--decode-style prose`）。

| 指標 | A: BLACKFOLD | B: GLM |
|---|---|---|
| Decode（散文、長い生成） | 93.7 tok/s | 81.0 tok/s |
| **本文区間のみの文字/s** | **139〜161 文字/s** | 測定不能（本文がバースト到着） |
| **実効文字/s**（reasoning 込み） | 8〜29 文字/s | 25.1 文字/s（散文4問の平均） |
| reasoning / 本文 の長さ比 | 8〜21 倍 | 6〜11 倍 |

A は reasoning が本文の 8〜21 倍あり、実効速度が本文速度の 1/8 まで落ちる。
**日本語の生成自体は速く（140〜160 文字/s）、遅く感じる原因は書く前の思考量**。

## タスク別（18問×3回、PASS/FAIL）

| タスク | A | B | A 平均時間 | B 平均時間 |
|---|---|---|---|---|
| code_count_words | 3/3 | 3/3 | 21.3s | **7.6s** |
| code_yen_to_int | 3/3 | 3/3 | **14.2s** | 47.0s |
| code_lru_cache | 3/3 | 3/3 | **5.8s** | 8.9s |
| extract_employee_json | 3/3 | 3/3 | **1.3s** | 3.0s |
| numeric_multi_step | 3/3 | 3/3 | **4.5s** | 4.4s |
| summary_constraint | 3/3 | 3/3 | **5.6s** | 9.7s |
| classify_sentiment | 3/3 | 3/3 | **1.4s** | 2.3s |
| long_context_needle | 3/3 | 3/3 | **2.1s** | 2.4s |
| code_bugfix_merge_intervals | 3/3 | 3/3 | **4.1s** | 9.3s |
| reconcile_invoices | 3/3 | 3/3 | **1.8s** | 3.2s |
| table_extract_filter | 3/3 | 3/3 | **2.3s** | 5.6s |
| business_days | 3/3 | 3/3 | **6.5s** | 14.4s |
| prose_explain | 3/3 | 3/3 | **30.2s** | 42.3s |
| prose_procedure | 3/3 | 3/3 | **20.5s** | 54.8s |
| **code_csv_parser** | **0/3** | **0/3** | 88.9s | 142.5s |
| **code_kanji_number** | **0/3** | **0/3** | 99.8s | 131.2s |
| **prose_email** | 3/3 | **0/3** | 38.9s | 104.1s |
| **prose_longform** | **0/3** | **0/3** | 85.8s | 104.3s |

## 失敗の内訳

- **code_csv_parser / code_kanji_number（両 SUT）**: 8192 トークンを reasoning で使い切り、本文（content）が空のまま `finish_reason=length`。文字8-gram 反復率 0.44〜0.61 で degeneration。**予算を 16384 にしても本文は出ない**（A で確認）。同じ2問を gpt-6.1-sol に解かせると 21.7s / 25.8s で PASS（約1,050文字のコードのみ）なので、タスク難度ではなく reasoning の停止不能。
- **prose_longform（両 SUT）**: 1,000〜1,400字を要求。A は 754字で 8192 を使い切って FAIL、B も 104s で予算切れ。B は 1,097〜1,172字を書ける run もあるが、3回とも FAIL（散文は予算に極端に敏感）。
- **prose_email（B のみ）**: 300〜500字のメール。B は reasoning に予算を使い切り、本文ゼロ。

## 注意（この表を公開するときに必ず添える）

1. **3回×2 SUT・単一マシン・単一経路**。SUT 間の差は統計的有意差ではない。attempt 単位の Wilson CI は naive iid（反復は相関する）。
2. **A の decode は 5.12 tok/chunk**。`R_out` は「見かけ」で、token ITL ではない。B の 1.01 tok/chunk とは意味が違う。
3. **散文の予算は 8192**。散文タスクの合否は予算の関数（2048/4096 では explain/email も本文ゼロ）。
4. **B の測定はモデル入れ替え込み**。cold TTFT 7.93s、warmup に 15 回試行。実行中に他クライアントが同じエンドポイントを使うと入れ替えが入る（実測で 166 秒のロード待ち）。
5. **B の本文区間の文字/s は測定不能**。本文がバーストで届き、区間長がほぼ 0 になるため。
6. **prefill はサーバー内部の純粋な prefill ではない**。ネットワーク・scheduler・tokenization を含む実効傾き。切片は内訳を分離した値ではない。
7. **OpenRouter の標準 1017 はこの表に入れていない**。proxy がチャンクをまとめて返す（48.55 tok/chunk、gap 242ms）ため速度値が歪む。検証用としては使える（散文 12/12 PASS、2問 2/6）。

## 生データ

- `results/spec-v2-blackfold/`（14問×3、manifest、attempts.jsonl、report.md）
- `results/spec-v2-prose8k/`（散文4問×3）
- `results/oss-glm/`（18問×3、manifest、attempts.jsonl、report.md）
- `results/openrouter-1017/`, `results/openrouter-1017-prose/`（検証用）
