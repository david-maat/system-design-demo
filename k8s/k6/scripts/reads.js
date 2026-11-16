// Step 2: read-heavy traffic (90% heavy reads, 10% writes). The primary DB (500m CPU)
// saturates on the unindexed /items/stats query. Route reads to the replica -> it recovers.
import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://demo-api.demo.svc.cluster.local';
const RATE = parseInt(__ENV.RATE || '25');

export const options = {
  scenarios: {
    reads: {
      executor: 'ramping-arrival-rate',
      startRate: 2,
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: 150,
      stages: [
        { target: RATE, duration: '1m' },
        { target: RATE, duration: '10m' },
        { target: 0, duration: '30s' },
      ],
    },
  },
};

export default function () {
  let res;
  if (Math.random() < 0.9) {
    res = http.get(`${TARGET}/items/stats`, { timeout: '5s', tags: { endpoint: 'stats' } });
  } else {
    res = http.post(`${TARGET}/items`, JSON.stringify({ name: `viral-${Date.now()}` }), {
      headers: { 'Content-Type': 'application/json' },
      timeout: '5s',
      tags: { endpoint: 'write' },
    });
  }
  check(res, { 'status 2xx': (r) => r.status >= 200 && r.status < 300 });
}
