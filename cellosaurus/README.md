# cellosaurus.txt の置き場 / where cellosaurus.txt goes

[Cellosaurus](https://ftp.expasy.org/databases/cellosaurus/) の
`cellosaurus.txt` をこのフォルダに置いてください。約 200 MiB あります。

```bash
curl -O https://ftp.expasy.org/databases/cellosaurus/cellosaurus.txt
```

HPA の配布ファイルとは更新サイクルが違うため、`HPA_SOURCE_DIR` とは別の
フォルダにしてあります。場所は `.env` の `HPA_CELLOSAURUS_DIR` /
`HPA_CELLOSAURUS_FILE`、Docker を使わない場合は `HPA_CELLEXP_CELLOSAURUS`
（または `build --cellosaurus`）で変えられます。

設定してあれば `build` に `--cellosaurus` を書かなくても使われます。
ファイルが無ければ黙ってスキップされます。
