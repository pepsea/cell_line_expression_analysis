# HPA 細胞株 遺伝子発現エクスプローラー

Human Protein Atlas (HPA) の **cell line RNA-seq データ**を使って、
「どの遺伝子が、どの細胞株に、どれくらい発現しているのか」を調べる Web アプリです。

- **遺伝子は複数まとめて入力できます** — シンボル (`EGFR`) でも Ensembl ID
  (`ENSG00000146648`) でも、カンマ・空白・改行のどれで区切っても構いません。
- **細胞株は由来臓器 (organ)・種 (species)・疾患 (disease)・名前で絞り込めます。**
- **細胞株名をクリックすると細胞株データベース ([Cellosaurus](https://www.cellosaurus.org/)) が開きます。**
  Cellosaurus accession (CVCL_xxxx) が判っていればその項目へ直接、
  判らなければ名前検索へリンクします。
- ヒートマップとテーブルの2つのビュー、TSV エクスポート、共有可能な URL。

<sub>ヒートマップは canvas で描画し、表示範囲のセルだけを塗るため、
20,000 遺伝子 × 1,200 細胞株の完全版データでも軽快に動作します。</sub>

---

## 1. セットアップ

```bash
python3 -m pip install -r requirements.txt
```

Python 3.9 以降が必要です。データ取り込み (`build`) は標準ライブラリだけで動くので、
FastAPI / uvicorn は Web サーバを立てるときにだけ必要です。

## 2. すぐ動かす（デモデータ）

実データを取り込む前に、UI の動作を確認できます。

```bash
python3 -m hpa_cellexp demo      # 合成データの小さな DB を作成
python3 -m hpa_cellexp serve     # http://127.0.0.1:8000
```

> ⚠️ **デモの発現値は合成値です。** 細胞株名・由来臓器・Cellosaurus ID は実在のもの
> ですが、数値は動作確認のための擬似乱数であり、実測値ではありません。
> デモ DB を使っている間は画面上部に警告バナーが出続けます。

## 3. Docker で常時稼働させる

サーバに常設する場合はこちらが簡単です。Python のインストールも不要です。

```bash
# 1) HPA のファイルを置いたディレクトリを指定して、DB を作る（初回のみ）
export HPA_SOURCE_DIR=/path/to/hpa-files
# --build を付けると、コード更新後でも必ず最新のイメージが使われます
docker compose run --rm --build build \
  --expression  /source/rna_celline.tsv.zip \
  --metadata    /source/cell_line_analysis_data.tsv.zip \
  --tcga        /source/rna_cell_line_tcga_comparison.tsv.zip \
  --cellosaurus /source/cellosaurus.txt \
  --release     "HPA v24"

# 2) 常設起動（以後、ホスト再起動時も自動で立ち上がります）
docker compose up -d

#    http://localhost:8000
```

### ポートを変更する

`HPA_CELLEXP_PORT` を変えるだけです。**恒久的に変えるなら `.env`** に書きます
（`docker compose` が自動で読み込みます）:

```bash
cp .env.example .env
# .env の HPA_CELLEXP_PORT=8000 を書き換える（例: 9000）
docker compose up -d          # → http://localhost:9000
```

1回だけなら環境変数でも構いません:

```bash
HPA_CELLEXP_PORT=9000 docker compose up -d
```

変えるのは**ホスト側のポートだけ**で、コンテナ内は常に 8000 で待ち受けます。
そのためポート変更でヘルスチェックやリバースプロキシ設定を直す必要はありません。
（`docker-compose.yml` の `"${HPA_CELLEXP_PORT:-8000}:8000"` の左がホスト側、右が
コンテナ側です。）

外部に直接公開せず、リバースプロキシ経由だけにしたい場合は `ports` を
ループバックに絞ります:

```yaml
    ports:
      - "127.0.0.1:${HPA_CELLEXP_PORT:-8000}:8000"
```

**compose を使わない場合**（`docker run`）は、いつも通りの `-p` で変えられます:

```bash
docker run -d --restart unless-stopped -p 9000:8000 \
  -v cellexp-data:/data hpa-cellexp:latest
```

`--network host` などでポートマッピングが使えない場合に限り、
コンテナ内の待ち受けポート自体を動かせます。ヘルスチェックも追従します:

```bash
docker run -d --network host -e HPA_CELLEXP_PORT=9000 \
  -v cellexp-data:/data hpa-cellexp:latest
```

**Docker を使わない場合**は `--port`（環境変数 `HPA_CELLEXP_PORT` でも可）:

```bash
python3 -m hpa_cellexp serve --host 0.0.0.0 --port 9000
```

### 設定一覧

| 設定 | 既定値 | 説明 |
|---|---|---|
| `HPA_CELLEXP_PORT` | `8000` | **ブラウザでアクセスするポート**（ホスト側） |
| `HPA_SOURCE_DIR` | `./hpa-source` | HPA ファイルの置き場（`/source` に読み取り専用でマウント） |
| `HPA_CELLEXP_WORKERS` | `1` | ワーカー数。DB は読み取り専用で開くため安全に増やせます |

`.env.example` をコピーして `.env` を作ると、上記をまとめて設定できます。

- `restart: unless-stopped` — クラッシュ時もホスト再起動時も復帰し、
  自分で `docker compose stop` したときだけ止まったままになります。
- **ヘルスチェック**は `/api/health`（ファイルの有無だけ）ではなく `/api/meta`
  を叩き、実際に DB を開けるかを確認します。DB が古い・壊れている場合は
  `unhealthy` として現れます。
- ログは 10 MiB × 3 世代でローテーションします（常設でディスクを食い潰さないため）。
- データベースは**イメージに含めず** named volume `cellexp-data` に置きます。
  完全版で 900 MiB 近くあるうえ、データ更新のたびにイメージを作り直さずに済みます。

### 運用コマンド

```bash
docker compose logs -f web                       # ログ
docker compose ps                                # 状態（healthy かどうか）
docker compose up -d --build                     # コード更新後の再デプロイ
docker compose run --rm --build build ...        # 取り込みも --build 付きが安全
docker compose run --rm build --version          # 動いているバージョンを確認
docker compose run --rm demo                     # デモ DB に差し替え
docker compose run --rm organs --grep caco       # 由来臓器の判定根拠を確認
docker compose run --rm inspect /source/xxx.zip  # 入力ファイルの列を確認
```

データを更新するときは `build` を実行してから `docker compose restart web` してください。

### データベースをホスト側に置きたい場合

`docker-compose.yml` の `cellexp-data:/data` を `./data:/data` に変えて、
コンテナ内ユーザ（uid 10001）が書けるようにします:

```bash
mkdir -p data && sudo chown -R 10001:10001 data
```

named volume のままファイルを取り出すこともできます:

```bash
docker compose cp web:/data/hpa_cellexp.sqlite ./hpa_cellexp.sqlite
```

### 公開する場合

コンテナは HTTP をそのまま出すだけです。インターネットに出す場合は
nginx / Caddy / Traefik などのリバースプロキシで TLS を終端し、
`ports` をループバックに絞って（「ポートを変更する」参照）プロキシ経由のみに
してください。アプリに認証機能はありません。

> ℹ️ このリポジトリの Docker 構成は、compose ファイルの妥当性・コンテナが実行する
> コマンド・ヘルスチェックのコマンドまで検証済みですが、**イメージのビルド自体は
> 未実行**です（作成環境に Docker デーモンが無いため）。
> 初回は `docker compose build` の出力を確認してください。

---

## 4. 実データを取り込む（Docker を使わない場合）

### 4.1 入力ファイル

HPA の配布ファイル（`.tsv` / `.tsv.zip` / `.tsv.gz` のいずれでも、解凍不要）:

| ファイル | 役割 | 必須 |
|---|---|---|
| `rna_celline.tsv` | 発現マトリクス（遺伝子 × 細胞株の TPM / pTPM / nTPM） | **必須** |
| `cell_line_analysis_data.tsv` | 細胞株のアノテーション（由来臓器・疾患・種・Cellosaurus ID） | 推奨 |
| `rna_cell_line_tcga_comparison.tsv` | 細胞株と TCGA がんの類似度 | 任意 |
| `cellosaurus.txt` | **細胞株の由来組織・Cellosaurus ID・種・性別・年齢**（[Cellosaurus](https://ftp.expasy.org/databases/cellosaurus/) より） | **強く推奨** |

### 4.2 取り込み

```bash
python3 -m hpa_cellexp build \
  --expression  path/to/rna_celline.tsv.zip \
  --metadata    path/to/cell_line_analysis_data.tsv.zip \
  --tcga        path/to/rna_cell_line_tcga_comparison.tsv.zip \
  --cellosaurus path/to/cellosaurus.txt \
  --release     "HPA v24"
```

`data/hpa_cellexp.sqlite` が作られます（`--database` で変更可）。

実測値（20,000 遺伝子 × 1,200 細胞株 = 2,400 万行、gzip 入力）:

| | |
|---|---|
| 取り込み時間 | **90 秒**（約 28 万行/秒） |
| 生成される DB | **891 MiB**（単一ファイル、`-wal` は残りません） |
| 取り込み時のピークメモリ | **323 MiB**（全行ストリーミング処理） |
| 1 遺伝子 × 1,200 細胞株のクエリ | 約 10 ms |
| 12 遺伝子 × 1,200 細胞株 | 約 22 ms |
| 200 遺伝子（上限）× 1,200 細胞株 | 約 190 ms |

### 4.3 起動

```bash
python3 -m hpa_cellexp serve --host 0.0.0.0 --port 8000
```

### 列名について

取り込み側は**列名を別名リストで解決**します。`Cell line` / `Cell_line` / `CELL-LINE` /
`CellLineName` はすべて同じものとして扱い（camelCase も分解します）、`TPM` しか無い
旧リリースもそのまま読めます。DepMap 由来の `OncotreeLineage` /
`OncotreePrimaryDisease` なども認識します。

**取り込み後、どの列が使われたかが必ず表示されます**:

```
columns in cell_line_analysis_data.tsv:
  cell line      Cell line
  organ          (見つかりません / not found)
  tissue         Site of origin
  disease        Histology
```

`organ` も `tissue` も `disease` も見つからない場合は、由来臓器がほとんど埋まりません。
その場合は列を明示指定してください:

```bash
python3 -m hpa_cellexp inspect path/to/cell_line_analysis_data.tsv.zip   # 列を確認
python3 -m hpa_cellexp build --expression ... --metadata ... \
    --cell-line-column "Cell line name" \
    --organ-column     "Site of origin" \
    --disease-column   "Histology"
```

### 由来臓器 (organ) はどこから来るか

優先順に決定し、**どの段で決まったかを記録**します（UI と `organs` コマンドで確認可）。

1. メタデータの organ 列
2. organ 列が `Intestine` のような広い分類の場合、疾患/組織名でより具体的な臓器へ細分化
   （例: `Intestine` + `Colorectal adenocarcinoma` → `Colon`）
3. メタデータの disease / tissue 列からのキーワード推定
   （`hpa_cellexp/reference/organ_keywords.tsv`）
4. **Cellosaurus**（`--cellosaurus` を指定した場合）— 下記参照
5. TCGA 比較ファイルの最上位がん種からの推定 — **※参考値**
   （`hpa_cellexp/reference/tcga_organ.tsv`、例 `LUAD` → `Lung`）
6. **内蔵の細胞株リストからの推定 — ※参考値**
   （`hpa_cellexp/reference/cell_line_organ.tsv`）

1〜4 はデータ由来、5〜6 は推定値で `※参考値` と明示されます。
6 はメタデータにも Cellosaurus にも情報が無いときの穴埋めで、CACO-2→結腸、NCI-H1650→肺 のように
既知の細胞株を名前で引きます。記号・空白は無視して照合し、`MDA-MB-*` のような
シリーズ指定もできます。**単なるデータファイルなので自由に追記・修正してください。**

#### 神経系はまとめて「脳 (Brain)」

`Nervous system` / `Central nervous system` / `CNS` / `Peripheral nervous system`
などの表記は、どれも **`Brain`（脳）** に寄せます。神経芽腫の細胞株
（Kelly, SH-SY5Y, SK-N-\* …）もここに入ります。

厳密な解剖学では神経芽腫は末梢（交感）神経系の腫瘍ですが、項目を分けると
**Kelly が `脳` に出てこなくなり、細胞株が消えたように見えます**。実際そういう
報告を受けたため、由来臓器としては1項目に統一しました。疾患列には
`Neuroblastoma` がそのまま残るので情報は失われません。

分けたい場合は `hpa_cellexp/reference.py` の `_ORGAN_ALIASES` から神経系の行を
外し、`labels_ja.tsv` に項目を追加してください。

どれにも当てはまらない細胞株は未設定となり、絞り込みでは「（未設定）」として選べます。
取り込み時に判定内訳が表示されます:

```
由来臓器の判定内訳:
     812  メタデータの organ 列
     193  Cellosaurus CVCL_0023 (In situ: Lung) など
     147  内蔵の細胞株リストから推定 ※参考値
      54  （判定できず / 未設定）
```

### 由来組織がわからない細胞株 — Cellosaurus を使う

**はい、`https://ftp.expasy.org/databases/cellosaurus/` を使うのが正解です。**
Cellosaurus (SIB) は細胞株の標準データベースで、15万株以上を収録しています。
発現データ側のメタデータに由来組織が無い場合、これが最も確実な情報源です。

```bash
curl -O https://ftp.expasy.org/databases/cellosaurus/cellosaurus.txt
python3 -m hpa_cellexp build --expression ... --cellosaurus cellosaurus.txt
```

`.gz` / `.zip` に圧縮したままでも読めます。約 150 MB ですが、
必要な細胞株だけを1パスで拾うためメモリはほとんど使いません。

取り込むのは以下です:

| Cellosaurus の項目 | 使い道 |
|---|---|
| `CC Derived from site:` | **由来臓器**（例 `In situ; Lung` → 肺） |
| `DI` (NCIt 疾患名) | 疾患。転移株では由来臓器の判定にも使用 |
| `AC` (CVCL_xxxx) | **Cellosaurus へのリンクが名前検索ではなく該当ページ直リンクになります** |
| `OX` | 種（`NCBI_TaxID=10090` → マウス など） |
| `SX` / `AG` | 性別・年齢 |

**転移由来の株の扱い**: `Derived from site` は「採取した場所」であって
「がんが発生した臓器」ではありません。たとえば MDA-MB-231 は
`Metastatic; Pleural effusion`（胸水）ですが、由来臓器は疾患名から**乳腺**と判定します。
`In situ` の場合のみ site をそのまま使います。

Cellosaurus はデータ由来なので `※参考値` は付きません。ただし**データセット側の
メタデータが優先**で、Cellosaurus はその穴埋めに使われます。

### 種 (species) について

HPA の細胞株リソースは全てヒト由来のため、種の列が無い場合は
`Homo sapiens` が入ります。種の列があればその値を正規化して使います
（`Human` → `Homo sapiens` など）。他種を含むデータセットでもそのまま動きます。

正規化は**日本語表記も対象**です（`ヒト`・`ヒト由来`・`人` → `Homo sapiens`、
`マウス` → `Mus musculus`）。データ側が日本語で書いていても、`ヒト` と
`Homo sapiens` が別々の項目としてファセットに並ぶことはありません。
種のファセットは学名だけを表示します（日本語ラベルは検索語としては有効です）。

---

## 5. 使い方

1. 左サイドバーに遺伝子を入力（複数可）。
2. 指標を選択 — **nTPM** が細胞株間比較の推奨値です。
3. 「発現を解析する」。
4. あとは絞り込むだけ — **フィルタを変更すると結果は自動で更新されます**
   （ボタンを押し直す必要はありません）。
5. ヒートマップ上でセルにホバーすると詳細、**細胞株名をクリックで Cellosaurus**。
   テーブルビューでは列見出しクリックでソート、細胞株名はリンクです。
   テーブルには**該当する細胞株がすべて出ます**（表示件数の上限はありません）。
   1,200 細胞株のような大きな表は少しずつ描画し、途中経過を表の上に出します
   （実測: 1,200 細胞株 × 60 遺伝子で約 1 秒、最初の行は 20ms 程度で出ます）。
6. 「TSV をダウンロード」で結果をそのまま解析に回せます。
   ファイル名には日時が入ります（`hpa_cell_line_expression_20260809_153012.tsv`）ので、
   繰り返しエクスポートしても上書きされず、いつ取得したものか後から分かります。

### ヒートマップの向き

由来臓器で並べているときは、**臓器が変わる位置に区切り線**が入ります。線は細胞株名の
列も含めてシート全幅に引かれ、通常の罫線より太くしてあります（行高が 16px まで詰まると
細い線は見落とされ、シート全体がひと続きのリストに見えてしまうため）。左の臓器名は
自分のグループの範囲内にだけ描かれ、背景の帯もそのグループの最終行で終わります。

**行 = 細胞株、列 = 遺伝子**の縦長レイアウトです。細胞株は数百〜千件、遺伝子は
数個〜数十個というデータの形に合わせてあり、細胞株名は左端に横書きで並ぶので
そのまま読めます。由来臓器でソートしているときは、さらに左に臓器名がグループ
見出しとして表示されます。行見出しと列見出しは固定表示のまま、縦にスクロール
します。セルが十分広いときは数値もセル内に直接表示されます。

### ヒートマップの横幅

**シートの横幅は画面内に収まる範囲が上限**です。遺伝子を増やしても右に伸びていくこと
はなく、列が狭くなります。ノート PC でも横スクロールせずに最後の遺伝子まで見えます。

| 遺伝子数 | 挙動 |
| --- | --- |
| 少ない | 細胞株名の右側にある領域の**半分**の幅に収める（数個の遺伝子が極端に幅広い帯にならないように） |
| 増えてくる | 半分に収まらなくなったら、残りの幅も使って**画面の右端まで**で止まる |
| さらに増える | 幅は変えずに**列を細くする**。列幅が 14px を切ると遺伝子名は間引いて表示（全部の名前はホバーで確認できます） |

実測（1280px のノート PC、44 遺伝子時点）: 6 遺伝子 → シート 568px / 列 51px、
20 遺伝子 → 862px / 30px、43 遺伝子 → 864px / 14px。いずれも横スクロールなし。

`hpa_cellexp/static/app.js` の `MAX_COL_WIDTH` / `COMFORT_COL_WIDTH` /
`MIN_COL_WIDTH` / `PLOT_WIDTH_FRACTION` で調整できます
（判定は `geneColumnWidth()` に集約）。

### 並び替え

「発現量が高い順 / 低い順」を選ぶと**基準**を指定できます。個別の遺伝子のほか、
全遺伝子をまとめた3つの基準が使えます。

| 基準 | 定義 | 用途 |
|---|---|---|
| **全遺伝子が満遍なく（最小値）** | 遺伝子ごとに 0–1 へ正規化し、その**最小値** | **すべての遺伝子が発現している細胞株**を探す。最も弱い遺伝子（律速）が高い細胞株だけが上位に来ます |
| **発現量割合の平均** | 遺伝子ごとに「表示中の細胞株の合計に対するその細胞株の**割合**」を求め、遺伝子間で平均 | その遺伝子の発現をどの細胞株が担っているかで並べる。高発現遺伝子でも低発現遺伝子でも同じ重み |
| **全遺伝子の平均（絶対値）** | 生の発現量の算術平均 | 絶対量での比較。ただし GAPDH のような高発現遺伝子に強く左右されます |

**発現量割合の平均**は、遺伝子 *g* について
`割合(g, 細胞株) = 発現量(g, 細胞株) ÷ Σ発現量(g, 表示中の全細胞株)` を求め、
遺伝子間で平均したものです。各遺伝子の割合は合計が必ず 1 になるため、
GAPDH のような高発現遺伝子も低発現マーカーもまったく同じ重みで効きます。
値は 0〜1 で、全細胞株の合計が 1 になります。

デモデータで `GAPDH, ALB, KLK3, PTPRC, MITF, GFAP` を指定した場合の上位:
HEP G2（ALB の大半を占める）、LNCAP（KLK3）、U-138 MG（GFAP）、A-431（MITF）。
全細胞株に広く発現する GAPDH はどの細胞株にも薄く分配されるため順位を支配しません。

> 「割合」の他の解釈は数学的に無意味になるため採用していません。
> 細胞株内で選択遺伝子に対する割合を取ると平均は必ず `1/遺伝子数` になり、
> 総転写産物に対する割合（nTPM ÷ 10⁶）は算術平均と完全に同じ順序になります。

**最小値**の正規化は各遺伝子について `log10(1+値) ÷ log10(1+その遺伝子の最大値)`
（最大値は**現在絞り込まれている細胞株の中での**最大値）です。
これによりハウスキーピング遺伝子と低発現マーカーが同じ重みで効きます。

いずれの集計でも、選択範囲全体で 0 の遺伝子は情報を持たないため除外します
（含めると全細胞株の最小値が 0 に張り付くため）。値が欠損している遺伝子も除外します。

> 「発現量の平均だと発現量が大きい遺伝子に左右される」場合は
> **全遺伝子が満遍なく（最小値）** を使ってください。
> 1つでも発現していない遺伝子があると順位が下がるため、
> 「全遺伝子が揃って発現している細胞株」がそのまま上位に並びます。

テーブルビューでは遺伝子の列見出しをクリックしても同じ操作ができ、
同じ列をもう一度クリックすると昇順・降順が入れ替わります。

### 使用データの確認

右上のデータセット表示（例 `HPA v24 · 20,090 遺伝子 × 1,206 細胞株`）をクリックすると、
**そのサイトがどのファイルから構築されたか**が表示されます。

- 発現マトリクス / 細胞株メタデータ / TCGA比較 の各ファイル名・絶対パス・サイズ・更新日時
- リリース名、収録内容、指標、構築日時、データベースのパス

取り込み時のファイル情報がデータベースに記録されるため、
「今表示されているのはどの版のデータか」を後からでも確認できます。

### 検索について

- **細胞株名**: スペース・ハイフン・スラッシュは無視して照合します。
  `hek293` → `HEK 293`、`mdamb231` → `MDA-MB-231`。
- **由来臓器・種**: HPA の値は英語ですが、UI では日本語ラベルを併記し、
  **どちらの言語でも検索できます**（`肺` でも `lung` でも `Lung` にヒット）。
  ラベルは `hpa_cellexp/reference/labels_ja.tsv` で管理しています。
  フィルタとして送られる値は英語の正規名のままです。
- **関連語での検索**: `labels_ja.tsv` の3列目に書いた語でもその項目が引けます。
  `神経芽腫` や `glioma` で **脳 (Brain)** が引けるのはこの仕組みです。
  これは**検索のみ**に効き、細胞株の分類そのものは変わりません。

色スケールは対数 (log10) です。「全遺伝子共通」は絶対量の比較用、
「遺伝子ごとに正規化」は発現パターンの比較用に切り替えられます。
実行するとURLのハッシュに条件が保存されるので、そのまま共有・ブックマークできます。

---

## 6. HTTP API

Web UI が使っているものと同じ API を直接叩けます。

| Endpoint | 説明 |
|---|---|
| `GET  /api/meta` | データセット情報と絞り込み用ファセット一覧 |
| `GET  /api/genes?q=EGF` | 遺伝子名のオートコンプリート |
| `GET  /api/cell-lines?organ=Lung&species=Homo+sapiens&q=NCI` | 細胞株の検索 |
| `POST /api/expression` | 発現マトリクス（JSON） |
| `POST /api/expression.tsv` | 同じ内容を TSV で |

```bash
curl -X POST localhost:8000/api/expression \
  -H 'Content-Type: application/json' \
  -d '{"genes": "EGFR, ERBB2, ESR1", "metric": "ntpm", "organs": ["Lung", "Breast"]}'
```

対話的なドキュメント: <http://127.0.0.1:8000/docs>

---

## 7. 構成

```
hpa_cellexp/
  __main__.py     CLI (build / inspect / demo / organs / serve)
  ingest.py       取り込み: TSV/ZIP をストリーミングして SQLite を構築
  sources.py      .tsv / .tsv.zip / .tsv.gz の透過リーダ
  columns.py      列名の別名解決（リリース間の差異を吸収）
  reference.py    TCGA→臓器、疾患テキスト→臓器、種の正規化、日本語ラベル、Cellosaurus URL
  cellosaurus.py  cellosaurus.txt のストリーミングパーサ（由来組織・CVCL・種など）
  queries.py      読み取り側のクエリ（スレッドごとの read-only 接続）
  api.py          FastAPI アプリ
  schema.sql      SQLite スキーマ
  demo.py         合成デモデータ
  reference/      参照テーブル (TSV):
                    organ_keywords.tsv   疾患/組織名 → 臓器のキーワード
                    cell_line_organ.tsv  既知の細胞株 → 臓器（穴埋め用）
                    tcga_organ.tsv       TCGAがん種 → 臓器
                    labels_ja.tsv        日本語ラベル＋表示順
  static/         フロントエンド（ビルド不要のプレーン HTML/CSS/JS）
tests/            取り込み・クエリ・API・UI・デプロイ構成のテスト
Dockerfile          常時稼働用イメージ（アプリのみ。DB は volume）
docker-compose.yml  web（常設）＋ build / demo / organs / inspect（単発）
.env.example        ポート等の設定テンプレート（コピーして .env に）
```

発現テーブルは `PRIMARY KEY (gene_id, cell_line_id)` の `WITHOUT ROWID` テーブルです。
1遺伝子分の行が物理的に連続して並ぶため、本アプリの中心的なクエリ
（「この N 個の遺伝子を全細胞株について」）が N 回の範囲スキャンで済みます。

## 8. テスト

```bash
python3 tests/test_pipeline.py    # 取り込み・クエリ・API
python3 tests/test_ui.py          # ブラウザ操作の回帰テスト
python3 tests/test_deployment.py  # Dockerfile / compose の整合性
```

UI テストには Playwright が必要です（未インストールなら自動的にスキップされます）:

```bash
pip install playwright && playwright install chromium
```

---

## トラブルシューティング

### `error: unrecognized arguments: --cellosaurus ...`

**動かしているコードが古いだけです。** オプションが増えたのに、古いチェックアウト
または古い Docker イメージが使われています。まず今動いているものを確認します:

```bash
python3 -m hpa_cellexp --version          # 例: hpa_cellexp 1.2.0 (schema v3) from ...
python3 -m hpa_cellexp build --help       # 使えるオプション一覧
```

- **Docker の場合**: `docker compose run` / `up` は**コードが変わってもイメージを
  作り直しません**。既存の `hpa-cellexp:latest` がそのまま使われます。

  ```bash
  git pull
  docker compose build                  # ← これが必要
  docker compose run --rm --build build --expression ... --cellosaurus ...
  ```

  取り込みコマンドに `--build` を付けておけば、以後この問題は起きません。

- **Docker を使わない場合**: `git pull` してから実行し直してください。
  仮想環境に `pip install` している場合は入れ直しが必要です。

同じことは他のオプション（`--organ-column` など）でも起こります。
`--version` の出力がドキュメントより古ければ、まず更新してください。

なお `--database` はサブコマンドの**前でも後でも**書けます
（`--database x demo` / `demo --database x` のどちらでも同じ）。

### 由来臓器で並べると細胞株が消える

並び替えで行が落ちることはありません。実際に起きているのはこの2つです。

1. **由来臓器が別の項目に分かれている。** これが Kelly の消えた原因でした。
   神経系はまとめて **脳 (Brain)** の1項目にしてあります（下記）。
2. **由来臓器が付いていない。** その細胞株はグループ見出し「（未設定）」に
   まとまります。`organs --grep <名前>` で確認できます。付いていなければ
   `hpa_cellexp/reference/cell_line_organ.tsv` に1行足すか、
   `--cellosaurus` を渡して取り込み直してください。

### 「データベースの形式が古いため再構築が必要です」と表示される

コードを更新するとデータベースの形式が変わることがあります。
`python -m hpa_cellexp build ...`（デモなら `demo`）で作り直してください。

形式が古いままだと、以前は**画面は普通に開くのに検索だけが失敗する**（= 検索が
壊れているように見える）状態になっていました。現在は起動時とAPIの応答で
明示的に再構築を促します。

### 由来臓器が付いている細胞株がほとんどない

まず取り込み時に表示される列マッピングを確認してください。`organ` / `tissue` /
`disease` がすべて `(見つかりません)` なら、メタデータが一切使われていません。
`--organ-column` などで列を明示指定すると解決します（「列名について」を参照）。

**最も効果的なのは `--cellosaurus cellosaurus.txt` を付けることです。**
Cellosaurus は15万株以上を収録しており、由来組織・Cellosaurus ID・種・性別・年齢を
まとめて埋められます（「由来組織がわからない細胞株 — Cellosaurus を使う」参照）。

それでも残る分は内蔵の細胞株リストから推定して穴埋めします（`※参考値` と明示）。
手元の細胞株が載っていなければ
`hpa_cellexp/reference/cell_line_organ.tsv` に追記してください。

### 由来臓器が想定と違う

その細胞株の臓器が**どこから決まったか**を表示できます。

```bash
python -m hpa_cellexp organs --grep caco     # 名前で絞り込み
python -m hpa_cellexp organs --organ Colon   # 臓器で絞り込み
```

判定根拠はヒートマップのツールチップと、テーブルの「由来臓器」列のツールチップ
にも出ます。優先順位は README 前半の「由来臓器はどこから来るか」を参照してください。
`TCGA類似度からの推定 ※参考値` と付いているものは、由来ではなく**発現プロファイル
の近さ**から推定した参考値です。メタデータファイルを渡すと正確になります。

臓器名は `hpa_cellexp/reference/organ_keywords.tsv` のキーワードで判定しており、
**上から順に最初に一致したものが採用されます**。想定と違う場合はこのファイルに
より具体的なパターンを**上の行に**追加してください。

## データの出典と引用

発現データは [Human Protein Atlas](https://www.proteinatlas.org/) に由来します。
利用の際は HPA のライセンス（CC BY-SA 3.0）と引用要件に従ってください。
細胞株のリンク先は [Cellosaurus](https://www.cellosaurus.org/) (SIB) です。
本リポジトリのコードは HPA / Cellosaurus とは無関係の独立したツールであり、
データそのものは含んでいません。
