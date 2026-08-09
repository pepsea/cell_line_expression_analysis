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

HPA はリリースごとに列名を変えることがあるため、取り込み側は
**列名を別名リストで解決** します（`Cell line` / `Cell_line` / `CELL-LINE` はすべて同じ、
`TPM` しか無い旧リリースもそのまま読める、など）。
手元のファイルの列構成は次で確認できます:

```bash
python3 -m hpa_cellexp inspect path/to/rna_celline.tsv.zip
```

想定外の列名だった場合は `hpa_cellexp/columns.py` の別名リストに追記してください。

### 由来臓器 (organ) はどこから来るか

優先順に:

1. メタデータファイルの organ / tissue 列
2. メタデータファイルの disease / tissue 列からのキーワード推定
   （`hpa_cellexp/reference/organ_keywords.tsv`）
3. TCGA 比較ファイルの最上位がん種からの推定
   （`hpa_cellexp/reference/tcga_organ.tsv`、例 `LUAD` → `Lung`）

いずれにも当てはまらない細胞株は organ 未設定となり、取り込み時に件数が警告表示されます。

### 種 (species) について

HPA の細胞株リソースは全てヒト由来のため、種の列が無い場合は
`Homo sapiens` が入ります。種の列があればその値を正規化して使います
（`Human` → `Homo sapiens` など）。他種を含むデータセットでもそのまま動きます。

---

## 4. 使い方

1. 左サイドバーに遺伝子を入力（複数可）。
2. 指標を選択 — **nTPM** が細胞株間比較の推奨値です。
3. 由来臓器・種・疾患・細胞株名で対象を絞り込み（対象件数がリアルタイム表示）。
4. 「発現を解析する」。
5. ヒートマップ上でセルにホバーすると値、**細胞株名をクリックで Cellosaurus**。
   テーブルビューでは列見出しクリックでソート、細胞株名はリンクです。
6. 「TSV をダウンロード」で結果をそのまま解析に回せます。

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
  __main__.py     CLI (build / inspect / demo / serve)
  ingest.py       取り込み: TSV/ZIP をストリーミングして SQLite を構築
  sources.py      .tsv / .tsv.zip / .tsv.gz の透過リーダ
  columns.py      列名の別名解決（リリース間の差異を吸収）
  reference.py    TCGA→臓器、疾患テキスト→臓器、種の正規化、Cellosaurus URL
  queries.py      読み取り側のクエリ（スレッドごとの read-only 接続）
  api.py          FastAPI アプリ
  schema.sql      SQLite スキーマ
  demo.py         合成デモデータ
  reference/      臓器マッピングの参照テーブル (TSV)
  static/         フロントエンド（ビルド不要のプレーン HTML/CSS/JS）
tests/            取り込み・クエリ・API のテスト
```

発現テーブルは `PRIMARY KEY (gene_id, cell_line_id)` の `WITHOUT ROWID` テーブルです。
1遺伝子分の行が物理的に連続して並ぶため、本アプリの中心的なクエリ
（「この N 個の遺伝子を全細胞株について」）が N 回の範囲スキャンで済みます。

## 7. テスト

```bash
python3 tests/test_pipeline.py          # 標準ライブラリのみ + fastapi/httpx
```

---

## データの出典と引用

発現データは [Human Protein Atlas](https://www.proteinatlas.org/) に由来します。
利用の際は HPA のライセンス（CC BY-SA 3.0）と引用要件に従ってください。
細胞株のリンク先は [Cellosaurus](https://www.cellosaurus.org/) (SIB) です。
本リポジトリのコードは HPA / Cellosaurus とは無関係の独立したツールであり、
データそのものは含んでいません。
