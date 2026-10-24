# 測定結果（2026-10-24）— 2 SUT、同一ハーネス版

A と B は**同じコード**（`dbac6ab` の修正を含む。B の manifest は docs のみ更新した `4ea2df5`）で、
同じ18問×3回・同じ予算で測った。数値は `results/*/attempts.jsonl` から再生成できる。
指標の定義は `specs/metrics.md`。

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
| ハーネス | `dbac6ab` | `4ea2df5`（コードは `dbac6ab` と同一） |

同じマシン・同じ経路で、**同時に1モデルだけ**ロードされる構成。A と B は別ランで、間にモデル入れ替え（約166秒）が入る。

## 速度3指標

| 指標 | A: BLACKFOLD | B: GLM |
|---|---|---|
| **Prefill**（`TTFT_any` 傾きの逆数） | **2,276 tok/s**（R²=0.972、切片 0.493s、9/9 有効） | **1,921 tok/s**（R²=0.997、切片 0.411s、9/9 有効） |
| **Decode**（散文 probe、`R_out`） | **93.7 tok/s**（gap p50 39.1ms、3.60 tok/chunk） | **81.0 tok/s**（gap p50 11.3ms、1.01 tok/chunk） |
| 日本語トークン効率（入力） | 0.764 tok/文字 | 0.820 tok/文字 |
| **Task 完了時間**（18問×3、全試行平均） | **25.1 s** | **38.7 s** |
| 成功率 | 45/54（83.3%） | 42/54（77.8%） |
| TTP（観測時間/成功） | 30.1 s | 49.7 s |
| コールドスタート TTFT | 0.19 s（resident 済み。cold ではない） | 8.12 s（15回試行で warm 判定） |

`TTFT_answer` は prefill probe では **A/B とも 0/9**（8 token では本文が出ない）。だから prefill の主変数は `TTFT_any`。

## Decode の比較は限定的

- **A**: 散文 probe で本文 524字を生成。3.60 tok/chunk。
- **B**: 同じ散文 probe だが**本文 0（reasoning のみ）**。1.01 tok/chunk。

同じ式 `R_out=(C-1)/(l-f_any)` でも、**生成した仕事量が違う**（A は本文あり、B は reasoning のみ）。
A 93.7 と B 81.0 を「日本語 decode の速度差」として読まない。B は 1 tok/chunk なので gap p50 11.3ms は
token 間隔に近いが、**証明ではない**（配信粒度の補助診断）。

## 日本語散文の速度

| 指標 | A | B |
|---|---|---|
| 本文到着区間レート（散文4問の中央値） | **153.6 文字/s**（129〜160） | **199.3 文字/s**（183〜215） |
| 実効レート（reasoning 込み、中央値） | **11.7 文字/s**（8.0〜28.6） | **10.8 文字/s**（10.3〜11.4） |
| reasoning / 本文 の長さ比 | 約 12.8 倍 | 約 11.6 倍 |

**本文だけを見ると B のほうが速い**（199 vs 154 文字/s）。しかし reasoning が長いため、
タスク完了までの実効レートは A と同程度（10.8 vs 11.7）。「思考量が遅さの原因」とは断定しない
（計数単位・tokenizer・配信待ちを分離していない）。

## 散文タスクの内訳

| タスク | A | B | A 本文/reasoning | B 本文/reasoning |
|---|---|---|---|---|
| prose_explain（400-600字） | 3/3（32.8s） | 3/3（42.2s） | 524字 / 4,597字 | 478字 / 5,613字 |
| prose_procedure（5段・500字+） | 3/3（21.3s） | 3/3（54.7s） | 588字 / 3,027字 | 565字 / 6,644字 |
| prose_email（300-500字） | **3/3**（40.0s） | **0/3**（103.9s） | 318字 / 6,820字 | 0字 / 15,120字 |
| prose_longform（1000-1400字） | **0/3**（88.3s） | **0/3**（104.8s） | 754字 / 11,986字（部分本文・打切り） | 0字 / 11,668字 |

## タスク別（18問×3回、PASS/FAIL）

| タスク | A | B | A 平均時間 | B 平均時間 |
|---|---|---|---|---|
| code_count_words | 3/3 | 3/3 | 20.6s | 7.6s |
| code_yen_to_int | 3/3 | 3/3 | 14.3s | 46.8s |
| code_lru_cache | 3/3 | 3/3 | 5.8s | 8.9s |
| extract_employee_json | 3/3 | 3/3 | 1.4s | 3.0s |
| numeric_multi_step | 3/3 | 3/3 | 6.7s | 4.4s |
| summary_constraint | 3/3 | 3/3 | 7.9s | 9.7s |
| classify_sentiment | 3/3 | 3/3 | 1.4s | 2.3s |
| long_context_needle | 3/3 | 3/3 | 2.4s | 2.4s |
| code_bugfix_merge_intervals | 3/3 | 3/3 | 4.9s | 9.3s |
| reconcile_invoices | 3/3 | 3/3 | 1.8s | 3.2s |
| table_extract_filter | 3/3 | 3/3 | 2.2s | 5.6s |
| business_days | 3/3 | 3/3 | 6.9s | 14.3s |
| prose_explain | 3/3 | 3/3 | 32.8s | 42.2s |
| prose_procedure | 3/3 | 3/3 | 21.3s | 54.7s |
| prose_email | 3/3 | 0/3 | 40.0s | 103.9s |
| **code_csv_parser** | **0/3** | **0/3** | 90.7s | 142.2s |
| **code_kanji_number** | **0/3** | **0/3** | 102.8s | 130.4s |
| **prose_longform** | **0/3** | **0/3** | 88.3s | 104.8s |

## 失敗の内訳（観測）

- **code_csv_parser / code_kanji_number（両 SUT）**: 8192 を reasoning で使い切り、本文 0 で `finish_reason=length`。文字8-gram 反復率 0.44〜0.61（degeneration の**診断候補**で証明ではない）。A は 16384 でも本文 0（その上限での観測）。
- **prose_longform**: A は 748字・4段落で打切り（部分本文あり）、B は本文 0 で打切り。
- **prose_email（B のみ）**: reasoning 15,120字で予算を使い切り、本文 0。
- 同じ2問を gpt-6.1-sol に解かせると 21.7s / 25.8s で PASS（約1,050字のコードのみ）。**別モデル・別経路の1回**で、難度の排除実験ではない。

## 注意（公開時に添える）

1. **3回×2 SUT・単一マシン・単一経路**。SUT 間の差は**検定も推定もしていない**（「有意差なし」ではない）。
2. **Decode の横並びは限定的**（A は本文あり、B は reasoning のみ、tok/chunk も違う）。
3. **Prefill は 3点**。傾きの CI なし、非線形の診断なし、cache は unknown。
4. **散文の合否は予算の関数**（散文 8192、probe 4096。2048/4096 では explain/email も本文 0 になった別 run あり）。
5. **A の 0.19s は resident 済み**で cold TTFT ではない。B は入れ替え込み（cold 8.12s、warmup 15回）。
6. **`mean_content_chars_per_s`（summary）は散文以外も混ぜた平均**。散文だけの値は上の表。
7. **OpenRouter 標準 1017 は表に入れていない**（proxy バッファで速度値が歪む。散文 12/12 PASS、2問 2/6 の検証用）。

## 次の改善（P1）

1. Prefill を 6〜8段階×3ブロックに増やし、傾きの CI と非線形診断を出す。
2. 散文タスクを多入力化（題材2つ×4型、対応を保ったまま順序ランダム化）。
3. 統計は多入力化後に cluster bootstrap / McNemar（現状の Wilson CI は naive iid）。
4. S3（`steady.py`）は補助・公開対象外と明記。

## 生データ

- `results/spec-v3-blackfold/`（A: 18問×3、manifest、attempts.jsonl、report.md）
- `results/spec-v3-glm/`（B: 18問×3）
- `results/oss-glm/`（B: 旧版の参考）
- `results/openrouter-1017/`, `results/openrouter-1017-prose/`（検証用）
