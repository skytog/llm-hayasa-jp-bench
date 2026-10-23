# 測定プロトコル（v0.1）

実装と運用の手順を固定する。ここに書かれた条件を満たさない測定は「参考値」であり、比較表に載せてはいけない。

## 1. シナリオ

v0.1 は3シナリオを定義する。実装済みは S1 / S2、S3 は P1。

| ID | シナリオ | 内容 | 主張できること |
|---|---|---|---|
| S1 | single-stream latency | 1本ずつ、前の完了後に次を送る | 単一リクエストの TTFT・出力レート・完了時間 |
| S2 | finite burst | c 本を同時投入し全終了まで待つ | その burst 条件での makespan throughput とレイテンシ劣化 |
| S3 | steady-state | closed-loop（補充あり）または open-loop（到着率 λ） | 定常容量・SLO goodput（P1） |

S2 の結果を「本番の処理能力」「最大スループット」と名乗ってはいけない。

### S3 の運用規則

- **workload は自然終了させる**。`finish_reason=stop` を確認する。全件 `length` 打ち切りだと「固定長生成の容量」を測っていることになり、タスク性能ではない
- 測定窓は固定（例 60s）。**窓内に完了したリクエストだけ**を throughput の分子にする。窓外に完了した drain は件数だけ記録する
- latency は**到着から完了まで**（クライアント側の待ち行列を含む end-to-end）。セマフォ待ちを latency から除くと過小評価になる
- **水準間に idle gap を置く**（例 20s）。前の水準の余熱・backlog が次を汚染する（実測で TTFT が 0.4s→8.9s まで持ち上がった）
- open-loop は **offered rate が容量未満**でないと待ち行列が発散する。容量を先に closed-loop で当たり、その 1/2〜3/4 の rate を測る。`achieved_rps` と `offered_rps` を必ず併記する
- 水準あたりの完了数が少ない（10〜30 件）と SLO 通過率が大きくぶれる。duration を延ばすか、通過率に Wilson CI を付ける

## 2. cold / warm / cache

- **cold**: モデル未ロード状態から最初のリクエスト。初回 TTFT を `cold_start_ttft` として別記録する
- **warm**: モデルが resident で、直前のリクエストが完了している状態。計測は warm で行う
- warm 判定は「TTFT < 3秒」のような**閾値ではなく**、SUT の resident 状態（サーバー統計または明示的な warmup 手順）で決める。閾値判定は正当な遅い SUT を排除する
- **cache-off / cache-on** を別トラックとして扱う。cache-off はサーバー設定または統計で確認する。確認できなければ `cache: unknown`

## 3. 反復とランダム化

- タスク: 各タスク `R` 回（既定 3）。**反復は独立標本ではなく cluster 内反復**として扱う
- 速度: 各入力長 3 ブロック以上、各ブロックで複数回
- 入力長の順序、モデルの実行順、タスクの実行順は**ブロック内でランダム化**し、seed を manifest に残す
- temperature 0 の同一問反復は「実行変動」の測定に使う。サンプリング由来の成功確率を測るなら、事前設定した温度・seed 集合で別トラックを設ける

## 4. timeout / retry / 失敗の扱い

- timeout は**事前に固定**し、manifest に書く
- **自動再送は既定で off**。再送するなら policy（対象エラー、最大回数、backoff）を仕様化し、attempt 単位で記録する
- 失敗は欠測として消さない。`e_i` に失敗確定時刻を入れ、状態（`http_error` / `timeout` / `truncated` / `empty` / `budget_exhausted`）を必ず記録する
- 棄却（遅いサンプルを除く）は禁止。除くなら事前規則を書き、除外した全件をログに残す

## 5. 予算（max_tokens）

- タスクごとに `max_tokens` を固定し、manifest に書く
- `finish_reason` を必ず取得する。`length` で最終回答が無い場合は `budget_exhausted`
- `finish_reason` が取得できない SUT では `finish_reason: unknown` とし、`budget_exhausted` と断定しない

## 6. manifest（必須記録項目）

```yaml
run_id: 20261023T180000
sut:
  name: DeepSeek-V4.1-Flash-1017-BLACKFOLD-TP2
  model_revision: <hash>
  quantization: <EXL3 4bpw など>
  tokenizer_hash: <hash>
  chat_template_hash: <hash>
  engine: {name: vllm, version: <ver>, args: [...]}
  gpu: {type: GB10, count: 2, driver: <ver>}
  parallelism: {tp: 2, ep: 1}
  speculative: {method: dflash2, tokens: 7}
  sampling: {temperature: 0.0, top_p: 1.0, seed: null}
  cache: {mode: off|on|unknown, hit_tokens_policy: report}
  context_limit: 262144
endpoint:
  base_url: <公開可能な識別子>
  client_version: <harness commit>
  network: <local|tailscale|lan>
protocol:
  scenario: S1|S2
  concurrency: [1,2,4,8,10]
  repetitions: 3
  timeout_s: 900
  retry: off
dataset:
  name: ja_smoke_v1
  hash: <sha256>
  task_count: 14
grader:
  version: <commit>
  contract: strict_v1
```

## 7. 生データ

- 全リクエストの `s, f_any, f_answer, l, e, N, C, C_vis, finish_reason, state`
- SSE の raw event（時刻付き、または受信バッファ単位の時刻）
- 最終回答全文（切り詰めない）。reasoning 全文（保存する場合）
- 採点結果と、採点器が例外を出した場合はその traceback
- `results.json` は集計ではなく**生データを含む**。集計表はここから再生成できること

## 8. 報告の作法

- 旧版の指標値は `deprecated` と明示し、新旧を混ぜた表を出さない
- 品質と速度は別軸で出す。品質の最低基準を満たした SUT 間でのみ速度比較する
- 単一の総合順位に潰さない。タスク群別品質、成功/失敗別時間、TTFT、完了時間の分布、拒否・エラー・打切り率を併記する
- 測っていないもの（費用・消費電力・可用性）は「対象外」と明示する
