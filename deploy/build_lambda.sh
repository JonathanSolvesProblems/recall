#!/usr/bin/env bash
# Build a Lambda deployment zip.
#
# Dependencies are fetched as Linux wheels rather than installed from the local
# environment: psycopg[binary] ships compiled binaries, and a Windows or macOS
# wheel will import fine here and fail on Lambda with a missing .so.
set -euo pipefail

cd "$(dirname "$0")/.."
BUILD=.build/lambda
ZIP=deploy/unsay-lambda.zip

rm -rf "$BUILD" "$ZIP"
mkdir -p "$BUILD"

python -m pip install \
  --quiet \
  --target "$BUILD" \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  "psycopg[binary,pool]>=3.2" "fastapi>=0.115" "pydantic>=2.9" \
  "pydantic-settings>=2.6" "httpx>=0.27" mangum boto3

# The application itself, minus anything the request path never touches.
cp -r unsay "$BUILD/unsay"

# The CockroachDB Cloud CA. sslmode=verify-full needs a root cert, and libpq
# looks in ~/.postgresql/root.crt by default, which does not exist on Lambda.
# Bundled here and pointed at explicitly via sslrootcert in the DSN.
curl -sS --max-time 60 -o "$BUILD/root.crt"   "https://cockroachlabs.cloud/clusters/${CRDB_CLUSTER_ID:-8f000271-5a82-40bc-83fa-f8f17ce742f3}/cert"
mkdir -p "$BUILD/web" && cp web/index.html "$BUILD/web/"
find "$BUILD" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -name "*.dist-info" -type d -prune -exec rm -rf {} + 2>/dev/null || true

# Python's zipfile rather than the zip(1) binary, which is not present on a
# stock Windows/Git Bash setup.
python - "$BUILD" "$ZIP" <<'PY'
import pathlib, sys, zipfile
build, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
out.parent.mkdir(parents=True, exist_ok=True)
total = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for f in sorted(build.rglob("*")):
        if f.is_file():
            z.write(f, f.relative_to(build))
            total += f.stat().st_size
print(f"zipped {out}  {out.stat().st_size/1e6:.1f} MB  (unzipped {total/1e6:.1f} MB)")
print("Lambda limits: 50 MB zipped for direct upload, 250 MB unzipped.")
if out.stat().st_size > 50e6:
    print("NOTE: over 50 MB zipped, so upload via S3 rather than directly.")
PY
