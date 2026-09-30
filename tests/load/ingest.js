// k6 equivalent of ingest_load.py: signed OEM webhook batches (1000 events each).
//   docker run --rm -i --network host -v $PWD/tests/load:/scripts grafana/k6 run /scripts/ingest.js
import http from 'k6/http'
import crypto from 'k6/crypto'
import { check } from 'k6'
import { SharedArray } from 'k6/data'

const SECRET = __ENV.SECRET || 'dev-kestrel-secret'
const URL = __ENV.URL || 'http://localhost:8001/v1/oem/kestrel/telemetry'
const vins = new SharedArray('vins', () => JSON.parse(open('./vins.json')))

export const options = {
  scenarios: {
    steady: { executor: 'constant-arrival-rate', rate: 100, timeUnit: '1s', duration: '2m', preAllocatedVUs: 200 },      // 100 batches/s = 100K ev/s
    burst: { executor: 'constant-arrival-rate', rate: 300, timeUnit: '1s', duration: '5m', startTime: '2m', preAllocatedVUs: 600 }, // 3x
  },
  thresholds: { http_req_failed: ['rate<0.001'], 'http_req_duration{expected_response:true}': ['p(95)<2000'] },
}

export default function () {
  const now = Date.now() / 1000
  const base = Math.floor(now * 8) * 1000 + __VU * 1_000_000 + __ITER
  const lines = []
  for (let i = 0; i < 1000; i++) {
    const vin = vins[(__VU * 1000 + i) % vins.length]
    lines.push(`${vin}|${now.toFixed(3)}|12.970000|77.590000|30.0|55.00|95.00|30.0|12345.00|-3.00||PERIODIC|${base + i}|`)
  }
  const body = lines.join('\n')
  const ts = String(Math.floor(now))
  const sig = crypto.hmac('sha256', SECRET, `${ts}.${body}`, 'hex')
  const r = http.post(URL, body, { headers: { 'Content-Type': 'text/plain', 'X-Timestamp': ts, 'X-Signature': sig } })
  check(r, { accepted: (res) => res.status === 202 || res.status === 429 })
}
