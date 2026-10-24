# 測定結果（2026-10-24）— 探索的 run 一覧

**これは正式な比較ベンチマークではない。** 複数のハーネス版で測った保存結果の一覧で、
A と B は**同じ版・同じ条件で測り直していない**。数値は生データから再生成できるが、
SUT 間の差を「優劣」として読むには再測定が必要（末尾「次の再測定」）。

指標の定義は `specs/metrics.md`。比較単位は **SUT**（重み・量子化・tokenizer・template・engine・GPU・並列・sampling・network）。

## run 一覧

| run | SUT | ハーネス版 | 試行 | 予算 |
|---|---|---|---|---|
| A | DeepSeek-V4.1-Flash-1017-BLACKFOLD-TP2（EXL3, GB10×2, TP2, Tailscale） | `dbac6ab`（散文grader/観測器のP0修正後） | 18問×3 = 54 | コード/散文 8192、JSON 4096、probe 散文4096 |
| B | GLM-5.3-Flash-Uncensored-TensorFold（TensorFold, GB10×2, Tailscale） | `1cfcf18`（修正前） | 18問×3 = 54 | 同上 |

A の manifest: `results/spec-v3-blackfold/manifest_*.json`、B: `results/oss-glm/manifest_*.json`。
engine は A が `unknown`（llama-swap recipe）、B が `llama-swap`。model/tokenizer/template revision は未確定。

## A（BLACKFOLD）の速度3指標

| 指標 | 値 | 条件 |
|---|---|---|
| **Prefill** | **2,276 tok/s**（R²=0.9717、切片 0.493s、9/9 有効） | 3点（500/2000/8000字）×3回。`TTFT_any` の**全有効対**回帰。`TTFT_answer` は 0/9（probe が本文を出さない） |
| **Decode**（散文 probe） | **93.7 tok/s**（gap p50 39.1ms、3.60 tok/chunk） | 400-600字の日本語散文を要求、4096 token |
| 本文到着区間レート | **139.3 文字/s** | `content_span > 0.05s` かつ content chunk ≥5 |
| 実効（reasoning込み） | **16.9 文字/s** | 同じ probe |
| 日本語トークン効率 | 0.764 tok/文字 | 当該 probe の `prompt_tokens / 指定 filler 文字数`（salt・指示込みの粗い比） |
| **Task 完了時間**（18問×3） | **25.1 s**（全試行平均） | 失敗・打切りも含む |
| 成功率 | 45/54（83.3%） | 状態: pass 45 / budget_exhausted 6 / wrong_answer 3 |
| TTP（観測時間/成功） | 30.1 s | 同上 |

**Prefill の R²=0.97 は 3点・全対回帰の値**。3点では折れ曲がりを診断できず、傾きの CI も出していない。
2,276 は「この3点・この条件での探索的逆傾き」で、仕様準拠の推定値ではない。

## B（GLM）の速度3指標（旧版ハーネス）

| 指標 | 値 | 条件 |
|---|---|---|
| Prefill | 1,886 tok/s（R²=0.991、切片 0.344s） | 3点×3回（旧実装: 長さ別中央値で回帰） |
| Decode | 81.0 tok/s（gap p50 11.3ms、1.01 tok/chunk） | **散文 probe だが本文 0（reasoning のみ）** |
| 日本語トークン効率 | 0.821 tok/文字 | 同上 |
| Task 完了時間 | 38.7 s | 18問×3 |
| 成功率 | 42/54（77.8%） | pass 42 / budget_exhausted 12 |

## Decode の横並びはできない

A の 93.7 tok/s は**本文 524字を生成した run**、B の 81.0 tok/s は**本文 0（reasoning のみ）の run**。
同じ式 `R_out=(C-1)/(l-f_any)` でも、仕事量も計数単位も違う。**A 93.7 と B 81.0 を「日本語 decode の速度差」として読まない**。
A は 3.60 tok/chunk、B は 1.01 tok/chunk で、配信粒度も違う。

## 散文タスク（A、修正版）

| タスク | 結果 | 平均時間 | 本文 | reasoning | 本文レート | 実効レート |
|---|---|---|---|---|---|---|
| prose_explain（400-600字） | 3/3 | 32.8s | 524字 | 4,597字 | 154〜160 字/s | 14.7〜16.7 |
| prose_procedure（5段・500字+） | 3/3 | 21.3s | 588字 | 3,027字 | 129〜153 字/s | 27.1〜28.6 |
| prose_email（300-500字） | 3/3 | 40.0s | 318字 | 6,820字 | 154〜156 字/s | 8.0 |
| prose_longform（1000-1400字） | **0/3** | 88.3s | 754字 | 11,986字 | 150〜151 字/s | 8.3〜8.7 |

- 本文到着区間レートの中央値は **153.6 文字/s**、実効の中央値は **11.7 文字/s**。
- reasoning は本文の **約12.8倍**（平均）。ただし「思考量が遅さの原因」と断定はしない（計数単位・tokenizer・配信待ちを分離していない）。
- prose_longform は 3回とも 748字・4段落で `wrong_answer`（予算 8192 を reasoning で使い切り、`finish_reason=length`）。**部分本文ありの打切り**。

## タスク別（A: 修正版、B: 旧版）

| タスク | A | B | A 平均時間 | B 平均時間 |
|---|---|---|---|---|
| code_count_words | 3/3 | 3/3 | 20.6s | 7.6s |
| code_yen_to_int | 3/3 | 3/3 | 14.3s | 47.0s |
| code_lru_cache | 3/3 | 3/3 | 5.8s | 8.9s |
| extract_employee_json | 3/3 | 3/3 | 1.4s | 3.0s |
| numeric_multi_step | 3/3 | 3/3 | 6.7s | 4.4s |
| summary_constraint | 3/3 | 3/3 | 7.9s | 9.7s |
| classify_sentiment | 3/3 | 3/3 | 1.4s | 2.3s |
| long_context_needle | 3/3 | 3/3 | 2.4s | 2.4s |
| code_bugfix_merge_intervals | 3/3 | 3/3 | 4.9s | 9.3s |
| reconcile_invoices | 3/3 | 3/3 | 1.8s | 3.2s |
| table_extract_filter | 3/3 | 3/3 | 2.2s | 5.6s |
| business_days | 3/3 | 3/3 | 6.9s | 14.4s |
| prose_explain | 3/3 | 3/3 | 32.8s | 42.3s |
| prose_procedure | 3/3 | 3/3 | 21.3s | 54.8s |
| prose_email | 3/3 | 0/3 | 40.0s | 104.1s |
| **code_csv_parser** | **0/3** | **0/3** | 90.7s | 142.5s |
| **code_kanji_number** | **0/3** | **0/3** | 102.8s | 131.2s |
| **prose_longform** | **0/3** | **0/3** | 88.3s | 104.3s |

## 失敗の内訳（観測）

- **code_csv_parser / code_kanji_number（両 SUT）**: 8192 を reasoning で使い切り、本文 0 で `finish_reason=length`。文字8-gram 反復率 0.44〜0.61（degeneration の**診断候補**で証明ではない）。A は 16384 でも本文 0（その上限での観測）。
- **prose_longform（両 SUT）**: 1000-1400字を要求。A は 748字・4段落で打切り、B は 104s で打切り。
- **prose_email（B のみ）**: 300-500字のメールで本文 0。
- 同じ2問を gpt-6.1-sol に解かせると 21.7s / 25.8s で PASS（約1,050字のコードのみ）。**別モデル・別経路の1回**で、難度の排除実験ではない。

## 注意（公開時に添える）

1. **3回×2 SUT・単一マシン・単一経路**。SUT 間の差は**検定も推定もしていない**（「有意差なし」ではない）。
2. **A と B はハーネス版が違う**（`dbac6ab` / `1cfcf18`）。同じ表で並べるのは探索的比較まで。
3. **Decode の横並び不可**（A は本文あり、B は本文なし、tok/chunk も違う）。
4. **Prefill は 3点**。傾きの CI なし、非線形の診断なし、cache は unknown。
5. **散文の合否は予算の関数**（A: 散文 8192、probe 4096。2048/4096 では explain/email も本文 0 になった別 run あり）。
6. **A の「コールドスタート 0.19s」は resident 済み**。cold TTFT ではない。B は入れ替え込み（cold 7.93s、warmup 15回）。
7. **`mean_content_chars_per_s`（summary）は散文以外のタスクも混ぜた平均**。散文だけの値は上の表。
8. **OpenRouter 標準 1017 は表に入れていない**（proxy バッファで速度値が歪む。散文 12/12 PASS、2問 2/6 の検証用）。

## 次の再測定（同じ版で A→B）

1. **B（GLM）を `dbac6ab` で18問×3**（30〜40分）。実行中は他クライアントがエンドポイントを使うと入れ替えが入るため、専有して回す。
2. Prefill を 6〜8段階×3ブロックに増やし、傾きの CI を出す（P1）。
3. 散文タスクを多入力化（題材2つ×4型）（P1）。
4. 統計は多入力化後に cluster bootstrap / McNemar（P1）。

## 生データ

- `results/spec-v3-blackfold/`（A: 18問×3、manifest、attempts.jsonl、report.md）
- `results/oss-glm/`（B: 18問×3）
- `results/spec-v2-blackfold/`, `results/spec-v2-prose8k/`（旧版の参考）
- `results/openrouter-1017/`, `results/openrouter-1017-prose/`（検証用）
