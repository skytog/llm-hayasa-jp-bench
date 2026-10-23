# 指標仕様（v0.1）

この文書は「何を測っているか」を確定させる。実装はこの定義に従い、定義に無い主張をしない。

## 0. 測定単位は SUT

比較単位はモデル名ではなく **SUT（System Under Test）** とする。

SUT = モデル重み + 量子化 + tokenizer + chat template + 推論エンジン（版・起動引数）+ GPU/ドライバ + 並列化（TP/EP）+ 投機的デコーディング設定 + sampling 設定 + ネットワーク経路。

「モデルAはモデルBより速い」ではなく「SUT-A は SUT-B より、この条件で速い」と書く。全構成要素は manifest に記録する（`protocol.md` §6）。

## 1. 観測契約（1リクエストあたり）

リクエスト i について、単調時計で以下を記録する。

| 記号 | 定義 |
|---|---|
| `s_i` | クライアントが送信を開始した時刻 |
| `f_i^any` | reasoning または最終回答の**最初の非空 delta** を受信処理した時刻 |
| `f_i^answer` | 最終回答 `content` の**最初の非空 delta** を受信処理した時刻 |
| `l_i` | 対象とする最後の非空 delta を受信処理した時刻 |
| `e_i` | 正常終端を検証した時刻。失敗時は失敗確定時刻 or 事前定義 deadline |
| `N_i` | サーバー申告の `prompt_tokens`（cache-hit 分を含む） |
| `C_i` | サーバー申告の `completion_tokens`（reasoning を含む） |
| `C_i^vis` | 可視最終回答のトークン数（取得できなければ未取得） |
| `Y_i` | 凍結した採点仕様に合格し、かつ正常終了したとき 1 |

**TTFT は2種類に分ける。**

- `TTFT_any = f^any - s`
- `TTFT_answer = f^answer - s`

reasoning をユーザーに表示しないサービスでは `TTFT_any` だけでは体感待ち時間を説明できないため、両方報告する。

`role` のみの delta、`usage` のみの chunk、空文字列は「最初のトークン」とみなさない。

## 2. Prefill：実効入力長傾き

### 2.1 定義

ブラックボックス API からサーバー側の prefill 区間だけを取り出すことはできない。したがって報告するのは **「実効入力長傾きの逆数」** であり、GPU の純粋な prefill スループットではない。

同一 SUT・無競合・warm・cache 状態を宣言した条件で、複数の入力長 `N` について `TTFT_answer` を測り、

```
TTFT_ij = alpha + beta * N_ij + u_block(j) + epsilon_ij
```

を近似モデルとして `beta` を推定する。報告値は

```
P_eff = 1 / beta        [tok/s]   （beta > 0 のときのみ）
```

`alpha` は**外挿切片**であり、ネットワーク・キュー・tokenization・最初の decode を個別に分離した値ではない。`alpha` を「ネットワーク往復時間」と名乗ってはいけない。

### 2.2 測定条件

- 入力長は想定運用範囲を覆う **6〜8段階**（例: 256 / 512 / 1k / 2k / 4k / 8k / 16k tokens）
- 各長さを **3ブロック以上**で反復し、長さの順序はブロック内でランダム化
- 全サンプルの `N`・`TTFT`・入力全文を保存する（丸めない）
- 棄却禁止。遅いサンプルも残す（棄却するなら事前規則を仕様に書き、全ログを残す）

### 2.3 診断と報告

- `beta` の 95% CI を、ブロックを保った bootstrap または適切な回帰推定で求める
- CI の両端が正なら `P_eff` の CI は `[1/b_high, 1/b_low]`。0 を跨ぐなら有限の速度 CI を報告しない
- 残差プロット・分散の不均一・折れ曲がりを確認する
- **線形性が崩れる場合**は単一の tok/s を強制せず、区間ごとの傾き、または TTFT 曲線そのものを主報告とする

線形性が崩れる代表条件：attention の長さ依存、kernel/CUDA graph の切替え、メモリ境界、KV eviction/offload、continuous batching と chunked prefill の交互実行、長さ依存の scheduler 待ち、prefix cache のヒット、最初の出力の束ね方の長さ依存。

### 2.4 prefix cache の扱い

- **cache-off トラック**と **cache-on トラック**を分ける。cache-off はサーバー設定または統計で確認する
- プロンプト先頭の salt は「user 本文より前の template/system prefix」のヒットを防がない。salt だけで cache-off を主張しない
- cache 状態が確認できない場合は `cache: unknown` と記録する

## 3. Decode：クライアント観測出力レート

### 3.1 定義

トークン単位の到着時刻が取れない場合、報告できるのは **クライアント観測の出力レート**である。

```
R_out = (C_i - 1) / (l_i - f_i^any)     [tok/s]
```

これは「最初の非空 delta から最後の非空 delta までの、サーバー申告トークン数による平均レート」であり、**TPOT の逆数ではない**。

### 3.2 チャンク間隔は ITL ではない

観測できるのは非空 delta の処理時刻 `a_1..a_K` であり、`a_j - a_{j-1}` は **inter-chunk gap** である。複数の SSE が1回の受信で届けば、実際の生成差ではなくクライアントの処理差が並ぶ。

したがって：

- チャンク間隔の統計は `inter-chunk gap` として報告する（median / p90 / p99 / 最大 stall）
- `ITL` / `TPOT` と名乗れるのは、トークン単位のタイムスタンプがある場合だけ
- 投機的デコーディングでは採用トークンがバーストするため、平均だけでなく stall とバーストサイズも記録する
- `usage` が無い場合、**チャンク数をトークン数とみなしてはならない**。`C: unknown` として扱う

### 3.3 可視文字レート

```
visible_chars/s = L_out^vis / (e_i - s_i)
```

`L_out^vis` は可視最終回答の Unicode code point 数。これは end-to-end の表示効率であり、decode-only の文字レートとは区別する。decode-only を主張するなら、分子の文字範囲と分母の時間範囲を一致させる（最初のチャンクを分母から除くなら、分子からも除く）。

## 4. タスク成功率

### 4.1 定義

凍結したタスク集合 `D`、入力・プロンプト・生成設定・予算・採点器バージョンを固定し、

```
p = E_(x~D)[Y(x)]
```

固定セットに対する推定は、事前に決めた重み `w_j` による

```
p_hat = sum_j w_j * mean_r(Y_jr)
```

等反復なら各タスク等重み。欠測や反復数が異なる場合は重みが崩れるので、欠測を状態として数える。

### 4.2 状態分類

試行は必ず次のいずれかに分類する。**PASS は「宣言した契約をすべて満たした」場合のみ。**

| 状態 | 意味 |
|---|---|
| `pass` | 契約を満たし、正常終了 |
| `wrong_answer` | 正常終了したが契約違反 |
| `format_violation` | 形式契約違反（JSONでない、余分なキー等） |
| `budget_exhausted` | `finish_reason=length` で最終回答が出ていない |
| `empty` | 最終回答が空 |
| `http_error` | 4xx/5xx |
| `timeout` | 事前定義の deadline 超過 |
| `truncated` | 終端検証に失敗（[DONE] 未到達、finish_reason なし） |
| `grader_error` | 採点器自体の例外 |

`endpoint 信頼性を含む成功率`（全試行の pass 率）と、`正常応答に限定した品質`（`pass / (pass+wrong_answer+format_violation)`）を併記する。

### 4.3 要約タスクの扱い

字数とキーワードしか見ていない場合、それは **制約通過率**であって要約品質ではない。意味採点を実装するまで `summary_constraint`（制約通過）と名乗る。

## 5. 時間あたりの成功（Time-to-Pass）

### 5.1 定義

```
TTP_obs = sum_i T_i / sum_i Y_i
```

`T_i = e_i - s_i` は全試行（失敗を含む）の所要時間。**失敗時間を欠測として平均から落としてはならない。**

### 5.2 何を主張できるか

- 主張できるのは「**観測消費時間あたりの成功件数**（全試行時間総和 ÷ 成功数）」という workload 効率
- 「同じ依頼を再試行したときの期待解決時間」を主張するには、再試行方針・最大回数・失敗検知器・累積コストを定義した policy が必要

再試行を仮定する場合、成功時平均 `mu_s`、失敗時平均 `mu_f`、成功率 `p` として

```
E[time_to_pass] = mu_s + ((1-p)/p) * mu_f = E[T]/p
```

が成り立つ（同一分布・独立な試行の更新過程、`p>0`、有限期待時間）。**時間 T と成否 Y の同一試行内の独立は必要ない**。検証・backoff・修正コストを T に含めていないなら、それぞれ `c_s`, `c_f` を足す。

- 成功0件は未定義（運用解釈では無限）。良い順位に並べない
- `mean_j(E[T_j]/p_j)` と `mean_j(E[T_j]) / mean_j(p_j)` は別物。タスク別 TTP と混合 TTP を分けて出す

## 6. スループットと goodput

### 6.1 burst（現行実装）

c 本を一括投入し全終了まで待つ方式は **finite burst の makespan throughput**。

```
throughput_burst = 正常完了件数 / (max(e) - min(s))
token_throughput_burst = 窓に帰属する生成トークン総数 / (max(e) - min(s))
```

これは「定常容量」でも「最大 goodput」でもない。`burst` と明記する。

### 6.2 定常（P1）

closed-loop（完了ごとに次を投入）または open-loop（到着率 λ で投入）で、観測窓 `W` を固定し、窓境界を跨ぐリクエストの帰属規則を事前に決める。warmup と drain を規定する。

### 6.3 goodput

```
G = sum_i 1[正常完了 AND Y_i=1 AND TTFT_answer_i <= tau_f AND T_i <= tau_e AND stall_i <= tau_stall] / W
```

閾値・入力分布・出力分布・到着率・拒否/deadline/再送方針を固定する。`offered / accepted / completed / quality-pass / SLO-pass` を併記し、accepted だけで分母を作らない。品質検証なしのレイテンシ制約だけなら **SLO goodput** と区別する。

## 7. 日本語の正規化

### 7.1 fertility

```
F_in  = N_text / L_in        （L_in は宣言した単位: code point など）
F_out = C_vis / L_out^vis
```

`N_text` は比較対象の日本語本文だけのトークン数。template を含む全 `prompt_tokens` の比は `F_total` として別に扱う（短い入力で template 差が混ざるため）。

NFC 正規化の有無、改行・空白・数字・記号を数えるかを仕様化する。NFKC は全角半角という問題自体を変えるので無条件にはかけない。code point / 書記素 / UTF-8 byte は別物として明記する。

### 7.2 比較トラック

- **日本語仕事量固定トラック**: 全 SUT に同じ日本語本文・同じ課題・同じ回答仕様を与える。入力文字数は同じでトークン数は異なる。TTFT・タスク時間・品質・可視 chars/s・fertility を報告する
- **token 長制御トラック**: 各 SUT の `N` に合わせた入力で backend の長さ依存を診断する。日本語仕事量比較とは呼ばない

`max_tokens` 固定は、tokenizer が違えば文字量も reasoning と回答への配分も変わるため、公平な仕事量固定ではない。API 上限・意味的完了条件・共通時間予算・回答長規約を分離して記述する。

## 8. 不確実性

- 二値・iid なら Wilson 区間を使ってよいが、**同一入力の反復を独立標本として扱ってはならない**
- 多様な入力への汎化を主張するなら、入力 cluster を再標本化する階層/層別 bootstrap
- 固定セットそのものが母集団なら、観測値はそのセットに対する記述値であり、任意の日本語タスク母集団への CI ではない
- 全成功データの単純 bootstrap は幅0になり得る。「真の成功率100%」の根拠にしない
- モデル間比較は対応を保つ（同一入力への成否は McNemar、時間は対応差/比 + CI）。多重比較は Holm
- 非有意は同等性の証明ではない


## 9. 日本語散文の decode（`--decode-style prose`）

数字列（ASCII）の出力では日本語の生成負荷が見えない。そこで 400-600字の日本語散文を出力させ、
次の2つを分けて記録する。

- **本文区間のみの文字/s** = `content_chars / (最後のcontent delta - 最初のcontent delta)`
  - reasoning を含めない、純粋な日本語生成の速さ
- **実効文字/s** = `content_chars / (最後のdelta - 最初のdelta)`
  - reasoning の時間を含む。ユーザーが本文を受け取るまでの実効速度

両者の比が **reasoning の相対コスト**になる（実測では reasoning が本文の 8〜20 倍の長さになり、
実効速度が本文区間の 1/8 程度まで落ちる）。`completion_tokens` は reasoning と本文の合計なので、
`content_chars / completion_tokens` を「日本語の文字/token」と読むのは誤り（reasoning で希釈される）。
