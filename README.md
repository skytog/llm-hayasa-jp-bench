# llm-bench-ja

日本語で記述した固定 workload を OpenAI 互換 API（`/v1/chat/completions`）に投げ、
**クライアント側で観測できる速度とタスク終結時間**を記録する実験用ハーネス。

評価対象はモデル単体ではなく **SUT**（モデル重み・量子化・tokenizer・chat template・推論エンジン・
GPU・並列化・sampling・ネットワークの全体）。同じモデル名でも構成が違えば別の SUT として扱う。

## 測るもの（主指標は3つ）

| 指標 | 意味 | 実装 | 主張の限界 |
|---|---|---|---|
| **Prefill** | 入力長あたりの TTFT 傾きの逆数 (tok/s) | プロンプト長を複数段階変え、`TTFT = a + b × prompt_tokens` を最小二乗フィット。`1/b` を採用 | サーバー内部の純粋な prefill ではない。ネットワーク・scheduler・buffering を含む。切片 `a` は外挿値で、内訳（往復/キュー待ち/固定費）は同定できない |
| **Decode** | クライアント観測出力レート `R_out` (tok/s) | `(completion_tokens - 1) / (最後の非空delta - 最初の非空delta)` | token 単位の ITL ではない。SSE チャンクには複数トークンが入る（実測 5.1 tok/chunk）。`inter-chunk gap` は別物として併記 |
| **Task completion time** | リクエスト開始から**終結**（成功・失敗・打切り）までの時間 | 固定タスクを N 回実行し、全試行の時間を平均 | 「成功したタスクの平均時間」ではない。成否は隣に並べて読む |

補助指標: タスク成功率（PASS/FAIL、**日本語能力の保証ではない**）、TTP（観測時間あたりの成功）、
日本語トークン効率（入力 `prompt_tokens / 文字数`。日本語は英語より同じ文字数で約3倍のトークンを食う）。

**指標の定義・仮定・観測契約は `specs/` が上位**（実装は仕様に追従する）:

- `specs/metrics.md` — TTFT_any / TTFT_answer、傾き法、R_out と inter-chunk gap、TTP、goodput、fertility
- `specs/protocol.md` — シナリオ、warm/cold、cache、反復、timeout、manifest、生データ、S3 の運用規則
- `specs/scoring.md` — 採点契約（最終行のみ、整数 exact、NaN 拒否、状態分類、隔離実行）
- `specs/result-schema.json` — 結果 JSON の形

## 主張できること / できないこと

**書いてよい**:

> 日本語の固定 workload に対する OpenAI 互換 API のクライアント観測レイテンシを記録する実験用ハーネス。
> 入力長に対する TTFT の傾き、API 申告トークン数と非空 delta の処理時刻による R_out、
> リクエストが終結するまでの時間を扱う。PASS/FAIL は固定契約への適合確認で、日本語能力を保証しない。

**書いてはいけない**:

- 「素の推論性能」（ネットワーク・scheduler・reasoning・tokenizer を含む）
- 「チャンク間隔 = token ITL」
- 「切片でネットワーク往復を分離した」
- 「salt により cache-off を保証した」（user 本文のみ。system/template prefix は残る。hit 統計が無ければ `unknown`）
- 「14問×3回で日本語の実利用全般を代表する」
- 「平均◯秒でタスクを達成する」（失敗・打切りの時間を含む終結時間）
- 「S3 の表が最大容量・本番同時ユーザー数」

## 隔離実行

- コードタスクの採点は **docker で隔離実行**（`--network none`, `--read-only`, tmpfs, memory 512m, pids 128, cpus 1, `--cap-drop ALL`, `no-new-privileges`）
- **期待値は親プロセスにのみ存在**する。候補は入力（`cases.json`）を読めるが正解を知らないので、正しく計算しない限り一致しない（nonce 文字列の偽装を根治）
- docker が無い場合、既定は **fail-closed**（`GraderUnavailable` → その run は `degraded`）。`LLM_BENCH_ALLOW_NO_SANDBOX=1` で subprocess にフォールバック（隔離なし）

## 生データ

- `manifest_<SUT>.json`: SUT 識別子（量子化・engine 版・GPU・TP・speculative）、sampling、cache mode、dataset hash、grader version、protocol
- `attempts.jsonl`: attempt 単位の `s/f_any/f_answer/l/e`、`N`、`C`、`usage`、`finish_reason`、`done_seen`、`parse_errors`、`state`、回答全文（`--no-full-text` で切詰め）
- `results.json` / `report.md`: 集計（生データから再生成できる）

SUT メタデータは `--sut-meta k=v` で渡す:

```bash
python3 bench.py --model X --sut-meta quantization=EXL3 --sut-meta engine=vllm \
  --sut-meta engine_version=0.11 --sut-meta gpu=GB10 --sut-meta gpu_count=2 \
  --sut-meta tp=2 --sut-meta network=tailscale --cache-mode unknown
```

## 使い方

```bash
# 1モデル、3回ずつ（probe も3回中央値）
python3 bench.py --model gemma-4-26B-A4B-it --runs 3

# probe の反復を増やす / 反復間の idle
python3 bench.py --model X --probe-reps 5 --probe-gap 3

# タスクだけ / 速度だけ
python3 bench.py --model X --tasks code_lru_cache,numeric_multi_step --runs 1
python3 bench.py --model X --no-speed

# S2 finite burst（makespan throughput。定常容量ではない）
python3 throughput.py --model X --concurrency 1,2,4,8

# S3 補助（定常窓・SLO goodput）
python3 steady.py --model X --concurrency 1,2,4,8 --duration 60 --gap 20
python3 steady.py --model X --mode open --rate 0.1,0.2,0.3 --duration 60
```

主なオプション:

- `--base-url`（既定: `LLM_BASE_URL` → PGX の Tailscale エンドポイント）
- `--runs`（既定 3）/ `--tasks`（`all` またはカンマ区切り）
- `--temperature`（既定 0.0）
- `--prefill-lengths`（既定 `500,2000,8000` 文字）
- `--probe-reps`（既定 3、中央値）/ `--probe-gap`（既定 2s）
- `--no-warmup` / `--no-speed` / `--no-full-text`
- `--cache-mode off|on|unknown` / `--scenario S1|S2|S3` / `--out`

## タスク一覧（14本）

| id | 種類 | 判定 |
|---|---|---|
| `code_count_words` | コード実装 | ランダム入力 11 ケース（全角スペース含む）を親側の期待値と照合 |
| `code_yen_to_int` | コード実装 | 全角数字・カンマ・円記号のランダムケース + ValueError |
| `code_lru_cache` | コード実装 | ランダムな put/get 列（容量1〜3）の戻り値列 |
| `extract_employee_json` | 情報抽出 | 氏名・部署・入社年（int exact）・年収（有限値） |
| `numeric_multi_step` | 数値推論 | 最終行の「答え:」のみ |
| `summary_constraint` | 制約付き生成 | 60文字以内 + 必須2語（**意味品質は見ない**） |
| `classify_sentiment` | 分類 | ラベル一致 + score 1..5 の整数 |
| `long_context_needle` | 長文抽出 | 3数値を int exact |
| `code_csv_parser` | コード実装（難） | クォート・`""`・クォート内改行のランダムケース |
| `code_bugfix_merge_intervals` | バグ修正（難） | 接する区間のマージ + **入力非破壊**（ランダム区間） |
| `code_kanji_number` | コード実装（難） | 漢数字（位取り・万億・一の省略・〇）のランダムケース |
| `reconcile_invoices` | 複数文書突合（難） | 請求総額・入金総額・未入金残高（int exact） |
| `table_extract_filter` | 表抽出（難） | プラス部署のみ、売上降順、要素は object |
| `business_days` | 日付計算（難） | 2026年4月の営業日数（最終行の整数） |

状態分類: `pass` / `wrong_answer` / `format_violation` / `budget_exhausted` / `empty` /
`truncated` / `http_error` / `timeout` / `grader_error`。`grader_error` が出た run は `degraded`。

## 注意点

- **warm/cold**: `wait_ready()` は TTFT < 3s を warm の代理指標にするだけで、resident 状態の確認ではない。最初の観測 TTFT を cold と呼ぶのも、モデル未ロードを確認した場合に限る
- **prefix cache**: prefill probe は反復ごとに salt を作り直す（同一プロンプトの2回目以降が cache に当たるのを防ぐ）。ただし user 本文の先頭だけなので system/template prefix の hit は防げない。cache 条件は `--cache-mode` の自己申告であり、サーバー統計で確認できなければ `unknown`
- **timeout**: 絶対 deadline（monotonic）で打ち切る。read timeout は idle 検出用に短く取る
- **usage 欠落**: `completion_tokens` が無い場合 `R_out` は unknown。チャンク数で代用しない。`degraded` として記録
- **Reasoning モデル**: `reasoning_content` も出力トークンとして数え、Task Time に含める。`max_tokens` はコード/数値=8192、JSON/要約=4096。最終回答（content）が空のまま `finish_reason=length` なら `budget_exhausted`
- **反復の独立性**: temperature 0 では出力がほぼ決定的（reasoning 文字数まで一致した実測あり）。attempt 単位の Wilson CI は naive iid で、**主表には出さない**（記述値として併記）
- **S2/S3 は補助**: S2 は finite burst の makespan throughput。S3 は測定窓内の完了だけを数え、SLO は到着基準 TTFT と end-to-end latency で判定し、`finish_reason=stop` のみを goodput に入れる

## 採点ロジックの自己テスト

```bash
python3 test_graders.py
# GOOD/BAD 14組 + 偽陽性の反例11件が期待通りなら "ALL GOOD"、終了コード0
```

既知の限界（`summary_constraint` は意味品質を見ない）は `KNOWN_LIMITATIONS` に明示している。
