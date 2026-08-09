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

## 3. 実データを取り込む

### 3.1 入力ファイル

HPA の配布ファイル（`.tsv` / `.tsv.zip` / `.tsv.gz` のいずれでも、解凍不要）:

| ファイル | 役割 | 必須 |
|---|---|---|
| `rna_celline.tsv` | 発現マトリクス（遺伝子 × 細胞株の TPM / pTPM / nTPM） | **必須** |
| `cell_line_analysis_data.tsv` | 細胞株のアノテーション（由来臓器・疾患・種・Cellosaurus ID） | 推奨 |
| `rna_cell_line_tcga_comparison.tsv` | 細胞株と TCGA がんの類似度 | 任意 |

### 3.2 取り込み

```bash
python3 -m hpa_cellexp build \
  --expression path/to/rna_celline.tsv.zip \
  --metadata   path/to/cell_line_analysis_data.tsv.zip \
  --tcga       path/to/rna_cell_line_tcga_comparison.tsv.zip \
  --release    "HPA v24"
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

### 3.3 起動

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
4. TCGA 比較ファイルの最上位がん種からの推定 — **※参考値**
   （`hpa_cellexp/reference/tcga_organ.tsv`、例 `LUAD` → `Lung`）
5. **内蔵の細胞株リストからの推定 — ※参考値**
   （`hpa_cellexp/reference/cell_line_organ.tsv`）

1〜3 はデータ由来、4〜5 は推定値で `※参考値` と明示されます。
5 はメタデータに情報が無いときの穴埋めで、CACO-2→結腸、NCI-H1650→肺 のように
既知の細胞株を名前で引きます。記号・空白は無視して照合し、`MDA-MB-*` のような
シリーズ指定もできます。**単なるデータファイルなので自由に追記・修正してください。**

どれにも当てはまらない細胞株は未設定となり、絞り込みでは「（未設定）」として選べます。
取り込み時に判定内訳が表示されます:

```
由来臓器の判定内訳:
     812  メタデータの organ 列
     193  疾患/組織名からの推定
     147  内蔵の細胞株リストから推定 ※参考値
      54  （判定できず / 未設定）
```

### 種 (species) について

HPA の細胞株リソースは全てヒト由来のため、種の列が無い場合は
`Homo sapiens` が入ります。種の列があればその値を正規化して使います
（`Human` → `Homo sapiens` など）。他種を含むデータセットでもそのまま動きます。

---

## 4. 使い方

1. 左サイドバーに遺伝子を入力（複数可）。
2. 指標を選択 — **nTPM** が細胞株間比較の推奨値です。
3. 「発現を解析する」。
4. あとは絞り込むだけ — **フィルタを変更すると結果は自動で更新されます**
   （ボタンを押し直す必要はありません）。
5. ヒートマップ上でセルにホバーすると詳細、**細胞株名をクリックで Cellosaurus**。
   テーブルビューでは列見出しクリックでソート、細胞株名はリンクです。
6. 「TSV をダウンロード」で結果をそのまま解析に回せます。
   ファイル名には日時が入ります（`hpa_cell_line_expression_20260809_153012.tsv`）ので、
   繰り返しエクスポートしても上書きされず、いつ取得したものか後から分かります。

### ヒートマップの向き

**行 = 細胞株、列 = 遺伝子**の縦長レイアウトです。細胞株は数百〜千件、遺伝子は
数個〜数十個というデータの形に合わせてあり、細胞株名は左端に横書きで並ぶので
そのまま読めます。遺伝子の列は、細胞株名の右側にある領域の**半分**の幅に
収まるよう配置します（遺伝子が多い場合は最小列幅が優先され、横スクロールします）。
幅の比率は `hpa_cellexp/static/app.js` の `PLOT_WIDTH_FRACTION` で変えられます。由来臓器でソートしているときは、さらに左に臓器名がグループ
見出しとして表示されます。行見出しと列見出しは固定表示のまま、縦にスクロール
します。セルが十分広いときは数値もセル内に直接表示されます。

### 並び替え

「発現量が高い順 / 低い順」を選ぶと**基準**を指定できます。個別の遺伝子のほか、
全遺伝子をまとめた3つの基準が使えます。

| 基準 | 定義 | 用途 |
|---|---|---|
| **全遺伝子が満遍なく（最小値）** | 遺伝子ごとに 0–1 へ正規化し、その**最小値** | **すべての遺伝子が発現している細胞株**を探す。最も弱い遺伝子（律速）が高い細胞株だけが上位に来ます |
| **全遺伝子の対数平均** | `log10(1+値)` の平均（= `1+値` の**幾何平均**） | 桁で効くため、算術平均ほど高発現遺伝子に引きずられません |
| **全遺伝子の平均（絶対値）** | 生の発現量の算術平均 | 絶対量での比較。ただし GAPDH のような高発現遺伝子に強く左右されます |

**対数平均**は絶対量の情報を保ったまま、影響を「倍率」に均します。
発現量 6,000 の遺伝子は 3.8、10 の遺伝子は 1.04 として効くため、
算術平均のように 1 個の高発現遺伝子だけで順位が決まることはありません。

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

色スケールは対数 (log10) です。「全遺伝子共通」は絶対量の比較用、
「遺伝子ごとに正規化」は発現パターンの比較用に切り替えられます。
実行するとURLのハッシュに条件が保存されるので、そのまま共有・ブックマークできます。

---

## 5. HTTP API

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

## 6. 構成

```
hpa_cellexp/
  __main__.py     CLI (build / inspect / demo / organs / serve)
  ingest.py       取り込み: TSV/ZIP をストリーミングして SQLite を構築
  sources.py      .tsv / .tsv.zip / .tsv.gz の透過リーダ
  columns.py      列名の別名解決（リリース間の差異を吸収）
  reference.py    TCGA→臓器、疾患テキスト→臓器、種の正規化、日本語ラベル、Cellosaurus URL
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
tests/            取り込み・クエリ・API のテスト
```

発現テーブルは `PRIMARY KEY (gene_id, cell_line_id)` の `WITHOUT ROWID` テーブルです。
1遺伝子分の行が物理的に連続して並ぶため、本アプリの中心的なクエリ
（「この N 個の遺伝子を全細胞株について」）が N 回の範囲スキャンで済みます。

## 7. テスト

```bash
python3 tests/test_pipeline.py    # 取り込み・クエリ・API
python3 tests/test_ui.py          # ブラウザ操作の回帰テスト
```

UI テストには Playwright が必要です（未インストールなら自動的にスキップされます）:

```bash
pip install playwright && playwright install chromium
```

---

## トラブルシューティング

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

メタデータ側に情報が無い場合でも、内蔵の細胞株リストから推定して穴埋めします
（`※参考値` と明示）。手元の細胞株が載っていなければ
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
