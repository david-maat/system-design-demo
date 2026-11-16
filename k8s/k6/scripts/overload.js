// Step 1: CPU-bound traffic. One API pod (200m CPU) tops out around ~15-20 req/s,
// so a fixed arrival rate above that makes latency + errors climb. Scale out -> it recovers.
import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://demo-api.demo.svc.cluster.local';
const RATE = parseInt(__ENV.RATE || '50');

export const options = {
  scenarios: {
    viral: {
      executor: 'ramping-arrival-rate',
      startRate: 5,
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: 400,
      stages: [
        { target: RATE, duration: '1m' },  // going viral
        { target: RATE, duration: '10m' }, // stay viral while we scale live
        { target: 0, duration: '30s' },
      ],
    },
  },
};

export default function () {
  const res = http.get(`${TARGET}/compute`, { timeout: '10s', tags: { endpoint: 'compute' } });
  check(res, { 'status 200': (r) => r.status === 200 });
}
