# llm-bench-ja

日本語での LLM レシピ比較用ベンチマーク。OpenAI 互換の `/v1/chat/completions`（vLLM 等）に対して使う。

## 測るもの

| 指標 | 意味 | 実装 |
|---|---|---|
| **Prefill** | 実効入力長傾きの逆数 (tok/s) | プロンプト長を 3 段階変えて TTFT を測り、`TTFT = a + b × prompt_tokens` を最小二乗フィット。`1/b` を prefill tok/s、`a` を固定オーバーヘッド（ネットワーク往復＋キュー待ち）として分離する |
| **Decode** | クライアント観測出力レート (tok/s) | ストリーミングのチャンク到着時刻からトークン間隔（ITL）を算出。平均・p50・p90 を出す |
| **Task Completion Time** | 実タスクの wall time | 固定日本語タスク 8 本を N 回実行し、リクエスト開始から完了までの実時間を測る |
| **Task Success Rate** | タスク成功率 | PASS/FAIL の二値。コードは実際にサブプロセスでテスト実行、抽出/分類は期待値比較、数値は許容誤差付き照合 |
| **TTP** | 観測消費時間あたりの成功 | `全試行時間総和 ÷ 成功数` |

Prefill / Decode は「素の推論性能」、Task Time / Success Rate / TTP は「実用性能」として分けて読む。

**指標の厳密な定義・仮定・観測契約は `specs/` を参照**（実装より仕様が上位）:

- `specs/metrics.md` — 指標の形式定義（TTFT_any / TTFT_answer、傾き法、R_out と inter-chunk gap の区別、TTP、goodput、日本語 fertility）
- `specs/protocol.md` — シナリオ・warm/cold・cache・反復・timeout/retry・manifest・生データ
- `specs/scoring.md` — 採点契約（strict schema / 最終行のみ / NaN拒否 / sentinel偽装対策 / 状態分類）
- `specs/result-schema.json` — 結果 JSON スキーマ

## 使い方

```bash
# 1モデル、3回ずつ
python3 bench.py --model gemma-4-26B-A4B-it --runs 3

# 複数モデル（順次実行。モデル入れ替え式サーバーでもOK）
python3 bench.py --model "A,B,C" --runs 3

# タスクだけ / 速度だけ
python3 bench.py --model X --tasks code_lru_cache,numeric_multi_step --runs 1
python3 bench.py --model X --no-speed
```

主なオプション:

- `--base-url`（既定: `LLM_BASE_URL` 環境変数 → PGX の Tailscale エンドポイント）
- `--runs`（既定 3）
- `--tasks`（`all` またはカンマ区切りの id）
- `--temperature`（既定 0.0）
- `--prefill-lengths`（既定 `500,2000,8000` 文字）
- `--no-warmup` / `--no-speed`
- `--out`（既定 `results/<timestamp>/`）

出力は `results.json`（生データ全部）と `report.md`（Markdown 表）。標準出力にも同じ表が出る。

## タスク一覧（14本）

| id | 種類 | 判定方法 |
|---|---|---|
| `code_count_words` | コード実装 | 単語数カウント関数を pytest 相当の assert で実行 |
| `code_yen_to_int` | コード実装 | 全角数字・カンマ・円記号を含む金額パース。ValueError も検査 |
| `code_lru_cache` | コード実装 | LRU キャッシュの eviction 順序まで検証 |
| `extract_employee_json` | 情報抽出 | 社員情報の JSON を期待値と比較（氏名・部署・入社年・年収） |
| `numeric_multi_step` | 数値推論 | 複数ステップの割合計算。最終行の「答え:」を照合 |
| `summary_constraint` | 制約付き生成 | 60文字以内 + 必須キーワード2語 |
| `classify_sentiment` | 分類 | ラベル一致 + score 閾値 |
| `long_context_needle` | 長文抽出 | 約3,000字の文書中の3数値を JSON で抽出 |
| `code_csv_parser` | コード実装（難） | クォート・`""` エスケープ・クォート内改行に対応した CSV パーサ |
| `code_bugfix_merge_intervals` | バグ修正（難） | 接する区間のマージ漏れ＋入力を破壊するバグを修正 |
| `code_kanji_number` | コード実装（難） | 漢数字（位取り・万億・先頭の一の省略）を整数化 |
| `reconcile_invoices` | 複数文書突合（難） | 請求書と入金明細（取消行あり）から請求総額・入金総額・未入金残高 |
| `table_extract_filter` | 表抽出（難） | Markdown 表からプラス部署のみ抽出し売上降順の JSON 配列 |
| `business_days` | 日付計算（難） | 2026年4月の営業日数（土日＋祝日除外） |

採点は PASS/FAIL のみだが、失敗は状態分類する（`wrong_answer` / `format_violation` / `budget_exhausted` / `empty` / `http_error` / `timeout` / `truncated` / `grader_error`）。`grader_error` が出た run は `degraded` として成功率の解釈から外す。

## 注意点

- **コールドスタート**: モデル入れ替え式のサーバーでは初回リクエストに 25〜60 秒かかる。`bench.py` は `wait_ready()` で TTFT が 3 秒未満になるまで最大 10 分待ってから計測し、初回 TTFT は `cold_start_ttft_s` として別記録する。ロード中に計測すると TTFT が数百秒になり prefill が負の傾きになるので必須。
- **Prefix caching 対策**: prefill プローブのプロンプト先頭にユニークな salt（資料番号）を入れている。同じ文章の繰り返しだと長いプロンプトが短いプロンプトの prefix cache をヒットし、prefill が実際より速く出る（実測で 3,278 → 2,114 tok/s のようにぶれる）。
- **TTFT の定義**: `max_tokens=1` だと vLLM は本文 `null` のチャンクを返すため、「最初の `choices` 付き SSE イベント到着時刻」を最初のトークン到着とみなしている。
- **Reasoning モデル**: `reasoning_content` も出力トークンとしてカウントし、Task Time に含める（実用上の待ち時間なので）。`max_tokens` は reasoning 分を見込んで多め（コード/数値=8192、JSON/要約=4096）。最終回答（content）が空のまま予算を使い切った場合は FAIL 扱い（reasoning 内のコードで偶然通るのを防ぐ）。
- **温度 0 の限界**: 出力が決定的なので 3 回実行しても出力・時間がほぼ同一になる。ばらつきを見たいときは `--temperature 0.3` などで。
- **並列実行しない**: 単一ストリームのレイテンシを見る設計。スループット（同時リクエスト）を見たい場合は別途 `--concurrency` を足す。
- **温度**: 既定 0.0。成功率のばらつきを見たいときは `--temperature 0.3` などで。

## 採点ロジックの自己テスト

LLM を介さず、正解コード/不正解コードを流し込んで grader を検証する:

```bash
python3 test_graders.py
# GOOD/BAD 14組 + 偽陽性の反例11件がすべて期待通りなら "ALL GOOD"、終了コード0
```
