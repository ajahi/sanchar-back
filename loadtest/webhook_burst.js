// "Boosted post" load test: a crowd of new customers DMs a few shops at once.
//
// What really hits the app is ONE request per DM: POST /webhooks/instagram. The customer/conversation/
// message rows are DB writes inside that handler, and the AI reply + Graph send are the app's own
// outbound calls (background task) -- so they are not requests this script sends. Mock them instead.
//
// Run (4 terminals, all from the repo root):
//   1. python -m loadtest.mock_external                         # fake Graph + Groq, LLM_LATENCY=2 by default
//   2. python -m loadtest.seed_tenants 20                       # 20 tenants -> loadtest/accounts.json
//   3. start the app with:  INSTAGRAM_APP_SECRET=loadtest-secret  INSTAGRAM_GRAPH_BASE=http://localhost:9000
//                           GROQ_URL=http://localhost:9000/chat/completions  (and a throwaway DATABASE_URL)
//   4. k6 run loadtest/webhook_burst.js                         # live view: K6_WEB_DASHBOARD=true k6 run ...
// Then: python -m loadtest.verify    (the webhook answers 200 even when ingest fails, so k6 alone can't see losses)
//
// Knobs (env): BASE_URL, APP_SECRET, PEAK (msgs/s, default 50), HOT_SHARE (traffic to the boosted shop, 0.6),
//              FOLLOW_UP (share of customers sending a 2nd message, 0.25), DUPLICATE (redelivered webhooks, 0.05)
import http from 'k6/http';
import crypto from 'k6/crypto';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8005';
const SECRET = __ENV.APP_SECRET || 'loadtest-secret';
const PEAK = Number(__ENV.PEAK || 50);
const HOT_SHARE = Number(__ENV.HOT_SHARE || 0.6);
const FOLLOW_UP = Number(__ENV.FOLLOW_UP || 0.25);
const DUPLICATE = Number(__ENV.DUPLICATE || 0.05);

const accounts = JSON.parse(open('./accounts.json')).accounts; // accounts[0] is the boosted shop
const RUN = Date.now();
const QUESTIONS = [
  'Simple chura ko price kati ho?', 'Is this available?', 'Delivery kati din lagchha?',
  'Hello', 'Yo stock ma chha?', 'Do you deliver outside Kathmandu?',
];

const messagesSent = new Counter('messages_sent');       // unique messages (compare with verify.py)
const duplicatesSent = new Counter('duplicates_sent');   // redeliveries of an already-sent mid

export const options = {
  scenarios: {
    boost: {
      // Open model: customers arrive at a rate no matter how slow the app gets (a closed loop would hide overload).
      executor: 'ramping-arrival-rate',
      startRate: 1, timeUnit: '1s', preAllocatedVUs: 200, maxVUs: 1000,
      stages: [
        { duration: '30s', target: Math.round(PEAK / 5) }, // warm-up
        { duration: '30s', target: PEAK },                 // post goes live: spike
        { duration: '2m', target: PEAK },                  // sustained
        { duration: '30s', target: 0 },                    // wind down
      ],
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    'http_req_duration{name:webhook}': ['p(95)<500', 'p(99)<1500'], // the ack must stay fast, Meta redelivers on timeouts
    checks: ['rate>0.99'],
  },
};

// 60% of customers go to the boosted shop, the rest spread over the others.
function pickAccount() {
  if (accounts.length === 1 || Math.random() < HOT_SHARE) return accounts[0];
  return accounts[1 + Math.floor(Math.random() * (accounts.length - 1))];
}

function send(account, igsid, mid, text, tag) {
  const body = JSON.stringify({
    object: 'instagram',
    entry: [{
      id: account, time: Date.now(),
      messaging: [{
        sender: { id: igsid }, recipient: { id: account }, timestamp: Date.now(),
        message: { mid, text },
      }],
    }],
  });
  const sig = 'sha256=' + crypto.hmac('sha256', SECRET, body, 'hex'); // same check as verify_webhook_signature
  const res = http.post(`${BASE_URL}/api/v1/webhooks/instagram`, body, {
    headers: { 'Content-Type': 'application/json', 'X-Hub-Signature-256': sig },
    tags: { name: 'webhook' },
  });
  check(res, { 'webhook 200': (r) => r.status === 200 });
  return res;
}

export default function () {
  const account = pickAccount();
  const igsid = `lt_c_${RUN}_${__VU}_${__ITER}`;          // every iteration is a brand-new customer
  const q = () => QUESTIONS[Math.floor(Math.random() * QUESTIONS.length)];

  const mid1 = `lt_${RUN}_${__VU}_${__ITER}_1`;
  send(account, igsid, mid1, q());
  messagesSent.add(1);
  if (Math.random() < DUPLICATE) { send(account, igsid, mid1, 'dup'); duplicatesSent.add(1); } // must not double-store

  if (Math.random() < FOLLOW_UP) {                        // thinks for 2-5s, then asks again (existing-customer path)
    sleep(2 + Math.random() * 3);
    send(account, igsid, `lt_${RUN}_${__VU}_${__ITER}_2`, q());
    messagesSent.add(1);
  }
}
