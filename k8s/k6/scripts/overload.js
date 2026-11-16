// Step 1: CPU-bound traffic. Measured: one API pod (200m CPU) serves ~8 req/s.
// 24 req/s = 3x one pod (latency + timeouts climb); 4 pods (~32 req/s) handle it with headroom.
import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://demo-api.demo.svc.cluster.local';
const RATE = parseInt(__ENV.RATE || '24');

export const options = {
  scenarios: {
    viral: {
      executor: 'ramping-arrival-rate',
      startRate: 5,
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: 150,
      stages: [
        { target: RATE, duration: '1m' },  // going viral
        { target: RATE, duration: '10m' }, // stay viral while we scale live
        { target: 0, duration: '30s' },
      ],
    },
  },
};

export default function () {
  const res = http.get(`${TARGET}/compute`, { timeout: '5s', tags: { endpoint: 'compute' } });
  check(res, { 'status 200': (r) => r.status === 200 });
}
