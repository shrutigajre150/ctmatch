#!/usr/bin/env bash
# Download TREC Clinical Trials 2021/2022 topics and relevance judgments from NIST.
# Usage: bash scripts/download_trec.sh data/trec
set -euo pipefail

OUT="${1:-data/trec}"
BASE="https://trec.nist.gov/data/trials"
mkdir -p "$OUT"

for f in topics2021.xml qrels2021.txt topics2022.xml qrels2022.txt; do
  if [[ -s "$OUT/$f" ]]; then
    echo "exists: $OUT/$f"
    continue
  fi
  echo "downloading $f"
  # -f: fail loudly on 404 (if NIST renames a file, check https://trec.nist.gov/data/trials2022.html)
  curl -fsSL "$BASE/$f" -o "$OUT/$f"
done

wc -l "$OUT"/qrels*.txt
