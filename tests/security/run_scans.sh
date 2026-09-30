#!/usr/bin/env bash
# Local security evidence: SAST (Semgrep), dependency/image/IaC scan (Trivy), DAST baseline (ZAP).
# Reports land in tests/security/reports/. Requires docker; the stack must be up for ZAP.
set -uo pipefail
cd "$(dirname "$0")/../.."
OUT=tests/security/reports
mkdir -p "$OUT"

echo "== Semgrep (SAST)"
docker run --rm -v "$PWD:/src" semgrep/semgrep semgrep scan \
  --config p/owasp-top-ten --config p/python --config p/typescript --config p/dockerfile --config p/secrets \
  --json -o /src/$OUT/semgrep.json /src/src /src/web/src /src/infra >/dev/null 2>&1
python3 -c "import json;r=json.load(open('$OUT/semgrep.json'));print(len(r['results']),'findings');[print(' ',x['extra']['severity'],x['check_id'].split('.')[-1],x['path'].replace('/src/',''),x['start']['line']) for x in r['results'][:40]]"

echo "== Trivy (python image, web image, filesystem incl. IaC misconfig + secrets)"
for img in voltpath/python:dev voltpath/web:dev; do
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD/$OUT:/out" aquasec/trivy:latest image \
    --severity HIGH,CRITICAL --ignore-unfixed --format table -o "/out/trivy-$(echo $img | tr '/:' '__').txt" "$img" >/dev/null 2>&1
  echo "  $img: $(grep -cE 'HIGH|CRITICAL' "$OUT/trivy-$(echo $img | tr '/:' '__').txt") HIGH/CRITICAL lines"
done
docker run --rm -v "$PWD:/src" -v "$PWD/$OUT:/out" aquasec/trivy:latest fs --scanners vuln,misconfig,secret \
  --severity HIGH,CRITICAL --format table -o /out/trivy-fs.txt /src >/dev/null 2>&1
echo "  filesystem: see $OUT/trivy-fs.txt"

echo "== OWASP ZAP API scan (DAST) against http://localhost:8000"
docker run --rm --network host -v "$PWD/$OUT:/zap/wrk" ghcr.io/zaproxy/zaproxy:stable zap-api-scan.py \
  -t http://host.docker.internal:8000/openapi.json -f openapi -r zap-report.html -J zap-report.json >/dev/null 2>&1
python3 -c "import json;r=json.load(open('$OUT/zap-report.json'));a=[x for s in r['site'] for x in s['alerts']];print(len(a),'alert types');[print(' ',x['riskdesc'],'-',x['name']) for x in a]" 2>/dev/null || echo "  ZAP report not produced (is the stack up?)"
