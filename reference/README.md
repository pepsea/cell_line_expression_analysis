# 参照テーブルの差し替え用フォルダ / reference table overrides

このフォルダに置いた TSV が、パッケージ同梱版より**優先**されます。
Which files? The four curated tables:

| ファイル | 内容 |
| --- | --- |
| `tcga_organ.tsv` | TCGA study code → 由来臓器・コホート名（英/日） |
| `labels_ja.tsv` | ファセット値の日本語ラベル、表示順、検索語 |
| `cell_line_organ.tsv` | 細胞株名 → 由来臓器（内蔵の推定リスト） |
| `organ_keywords.tsv` | 疾患・組織名のキーワード → 由来臓器 |

**ファイル単位で効きます。** 1つ置けばそれだけが差し替わり、残りは同梱版が
使われるので、全部コピーする必要はありません。

同梱版は `hpa_cellexp/reference/` にあります。まずコピーしてから編集してください。

```bash
cp hpa_cellexp/reference/tcga_organ.tsv reference/
```

場所は `HPA_CELLEXP_REFERENCE_DIR`（または `--reference-dir`）で変えられます。
Docker では `.env` の `HPA_REFERENCE_DIR` がこのフォルダを指します。
編集後は `docker compose restart web`（サーバは起動時に読み込みます）。
由来臓器の判定に関わるファイルを変えた場合は `build` のやり直しが必要です。
