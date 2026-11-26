#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

RESULTS_DIR=results
SITE_DIR=site
MAX_IMAGES=6
REFRESH=0
CSV=

usage() {
  cat <<'EOF'
Build the static report site in site/.

  1) run the Rightmove search (new or re-priced listings since the last run)
  2) generate the HTML report for the resulting CSV
  3) copy it to site/index.html

Usage: ./build_site.sh [options]

  --csv PATH        Skip the search and build from an existing CSV
  --max-images N    Images per property (default: 6)
  --refresh         Refetch listing details instead of using the cache
  -h, --help        Show this help

Exit codes: 0 site built, 2 search found nothing new (site unchanged), 1 error.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --csv) CSV=${2:?--csv needs a path}; shift 2 ;;
    --max-images) MAX_IMAGES=${2:?--max-images needs a number}; shift 2 ;;
    --refresh) REFRESH=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -x .venv/bin/python ]]; then
  PYTHON=.venv/bin/python
else
  PYTHON=$(command -v python3 || command -v python)
fi

list_csvs() {
  ls -1 "$RESULTS_DIR"/rightmove_*.csv 2>/dev/null | sort || true
}

if [[ -z "$CSV" ]]; then
  before=$(list_csvs)
  echo "==> Running property search"
  "$PYTHON" rightmove_search.py
  CSV=$(comm -13 <(printf '%s\n' "$before") <(list_csvs) | tail -n 1)
  if [[ -z "$CSV" ]]; then
    echo "No new or re-priced listings since the last run; site unchanged." >&2
    exit 2
  fi
fi

if [[ ! -f "$CSV" ]]; then
  echo "CSV not found: $CSV" >&2
  exit 1
fi

html="${CSV%.csv}.html"
report_args=("$CSV" --output "$html" --max-images "$MAX_IMAGES")
[[ "$REFRESH" -eq 1 ]] && report_args+=(--refresh)

echo "==> Generating report from $CSV"
"$PYTHON" generate_report.py "${report_args[@]}"

mkdir -p "$SITE_DIR"
cp "$html" "$SITE_DIR/index.html"
echo "==> Site ready: $SITE_DIR/index.html"
