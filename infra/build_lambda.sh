#!/usr/bin/env bash
# Build the Lambda deployment package: app code + runtime deps + the profile, zipped.
#
# boto3 is already in the Lambda python runtime, so only httpx (and its pure-Python deps —
# httpcore, h11, certifi, idna, anyio, sniffio) get vendored. All are pure Python, so building on
# this x86 box produces an artifact that runs unchanged on Lambda; no manylinux wheel juggling.
#
# Terraform points aws_lambda_function.filename at dist/lambda.zip and hashes it, so a rebuilt zip
# with changed bytes redeploys on the next apply. Run this before `terraform apply`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/dist/lambda_build"
ZIP="$ROOT/dist/lambda.zip"

rm -rf "$BUILD" "$ZIP"
mkdir -p "$BUILD"

# Runtime deps only (NOT the dev/test tools). boto3 is provided by Lambda. uv, not pip: the
# project's venv is uv-managed and ships no pip. Target Python 3.13 to match the Lambda runtime.
uv pip install --quiet --python 3.13 --target "$BUILD" "httpx==0.28.1"

# App code + the profile the scorer reads (runner.load_profile looks for it in the CWD = /var/task).
cp -r "$ROOT/src/aws_job_streamer" "$BUILD/"
cp "$ROOT/profile.example.json" "$BUILD/"

# Drop caches so the hash is stable and the artifact is lean.
find "$BUILD" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$BUILD" -type d -name "*.dist-info" -prune -exec rm -rf {} +

# Python's zipfile, not the `zip` CLI (not installed on this box). Contents land at the archive
# root (Lambda needs aws_job_streamer/ and httpx/ at the top level, not nested).
"$ROOT/.venv/bin/python" - "$BUILD" "$ZIP" <<'PY'
import sys, shutil
build, zip_path = sys.argv[1], sys.argv[2]
shutil.make_archive(zip_path[:-4], "zip", build)
PY
echo "built $ZIP ($(du -h "$ZIP" | cut -f1))"
