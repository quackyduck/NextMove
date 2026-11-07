// Same-origin module Worker (CSP default-src 'self' allows it): solves one proof-of-work challenge.
import { search } from "./sha256.js";

const BATCH = 4096;

self.onmessage = (event) => {
  const { challenge, bits } = event.data;
  for (let start = 0; ; start += BATCH) {
    const nonce = search(challenge, bits, start, BATCH);
    if (nonce >= 0) {
      self.postMessage({ nonce: String(nonce) });
      return;
    }
  }
};
