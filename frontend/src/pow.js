// Proof-of-work for requests that send email (sign-up, activation, resend, password reset).
// The server signs a challenge; the browser spends about a second finding a nonce, which makes
// spending the email quota from a script slow. Nothing leaves the browser but the nonce.
import { api } from "./api.js";
import { search } from "./sha256.js";

const MAIN_THREAD_BATCH = 2048;

function solveInWorker(challenge, bits, signal) {
  return new Promise((resolve, reject) => {
    let worker;
    try {
      worker = new Worker(new URL("./pow.worker.js", import.meta.url), { type: "module" });
    } catch (error) {
      reject(error);
      return;
    }
    const stop = () => {
      worker.terminate();
      reject(new DOMException("Cancelled", "AbortError"));
    };
    signal?.addEventListener("abort", stop, { once: true });
    worker.onmessage = (event) => {
      signal?.removeEventListener("abort", stop);
      worker.terminate();
      resolve(event.data.nonce);
    };
    worker.onerror = (event) => {
      signal?.removeEventListener("abort", stop);
      worker.terminate();
      event.preventDefault?.();
      reject(new Error("Worker unavailable"));
    };
    worker.postMessage({ challenge, bits });
  });
}

// Fallback without Workers: batches on the main thread, yielding so the page stays responsive.
async function solveOnMainThread(challenge, bits, signal) {
  for (let start = 0; ; start += MAIN_THREAD_BATCH) {
    if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    const nonce = search(challenge, bits, start, MAIN_THREAD_BATCH);
    if (nonce >= 0) return String(nonce);
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

export async function solve(challenge, bits, { signal } = {}) {
  try {
    return await solveInWorker(challenge, bits, signal);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    return solveOnMainThread(challenge, bits, signal);
  }
}

// Fetch a challenge for `purpose` and solve it: resolves to the `proof` field for the request body.
export async function solveChallenge(purpose, { signal } = {}) {
  const issued = await api(`/auth/challenge?purpose=${encodeURIComponent(purpose)}`, { signal });
  const nonce = await solve(issued.challenge, issued.bits, { signal });
  return { challenge: issued.challenge, nonce };
}

// Start solving as soon as a form is first used, so the proof is usually ready by submit.
// Returns a function that resolves to a fresh proof (a used or failed one is never reused).
export function prefetchProof(purpose) {
  let pending = null;
  return {
    warm() {
      if (!pending) pending = solveChallenge(purpose).catch(() => null);
    },
    async take() {
      const ready = pending ? await pending : null;
      pending = null;
      return ready || solveChallenge(purpose);
    },
  };
}
