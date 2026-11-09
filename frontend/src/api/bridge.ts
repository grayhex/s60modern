/**
 * Browser → Windows Bridge on 127.0.0.1. Results are classified, never thrown as raw text:
 * the wizard maps each kind to a concrete fix. Nothing here writes to a phone.
 */
export const BRIDGE_URL = 'http://127.0.0.1:24652/api/bridge/pair';

export type ProbeResult =
  | { kind: 'ok'; version: string | null; pairAvailable: boolean }
  | { kind: 'unreachable' } // not running, firewall, or the browser blocked local-network access
  | { kind: 'origin' }; // reachable, but this SymPort origin / Host is not acceptable

export type PairResult =
  | { kind: 'ok'; version: string | null }
  | { kind: 'declined' } // the operator pressed "no" on the PC
  | { kind: 'wait' } // too soon after the previous request
  | { kind: 'busy' } // pairing is unavailable or another request is open
  | { kind: 'timeout' }
  | { kind: 'unreachable' }
  | { kind: 'invalid' };

function parse(raw: unknown): Record<string, unknown> {
  return raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
}
const text = (value: unknown) => (typeof value === 'string' ? value.slice(0, 40) : null);

async function post(body: Record<string, unknown>, timeout: number) {
  const response = await fetch(BRIDGE_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'omit',
    cache: 'no-store',
    redirect: 'error',
    signal: AbortSignal.timeout(timeout),
    body: JSON.stringify(body),
  });
  const data = parse(await response.json().catch(() => null));
  return { status: response.status, data };
}

/** Reachability only: the Bridge answers without opening an approval dialog. */
export async function probeBridge(origin: string): Promise<ProbeResult> {
  try {
    const { status, data } = await post({ origin, probe: true }, 5000);
    if (status === 200 && data.probe === true)
      return {
        kind: 'ok',
        version: text(data.version),
        pairAvailable: data.pair_available === true,
      };
    // 400/403 from an answering Bridge: the origin or Host header is not acceptable. A Bridge
    // build without probe support also lands here; pairing will tell the difference.
    return { kind: 'origin' };
  } catch {
    return { kind: 'unreachable' };
  }
}

/** Opens the approval dialog on the PC. The lease token is created by SymPort, not the browser. */
export async function pairBridge(
  origin: string,
  token: string,
  caSha256?: string,
): Promise<PairResult> {
  try {
    const { status, data } = await post(
      { origin, token, ...(caSha256 ? { ca_sha256: caSha256 } : {}) },
      90000,
    );
    if (status === 200 && data.paired === true) return { kind: 'ok', version: text(data.version) };
    if (status === 403) return { kind: 'declined' };
    if (status === 429) return { kind: 'wait' };
    if (status === 409) return { kind: 'busy' };
    return { kind: 'invalid' };
  } catch (error) {
    return error instanceof DOMException && error.name === 'TimeoutError'
      ? { kind: 'timeout' }
      : { kind: 'unreachable' };
  }
}

/** What a paired Bridge reports about its own link to SymPort (no URLs, no secrets). */
export type RelayState = { state: 'connecting' | 'connected' | 'error'; reason: string | null };

export async function bridgeRelayState(origin: string): Promise<RelayState | null> {
  try {
    const { status, data } = await post({ origin, relay_status: true }, 5000);
    const relay = parse(data.relay);
    const state = text(relay.state);
    if (status !== 200 || (state !== 'connecting' && state !== 'connected' && state !== 'error'))
      return null;
    return { state, reason: text(relay.reason) };
  } catch {
    return null;
  }
}

/** SHA-256 of SymPort's own CA (Home LAN HTTPS), so the Bridge can pin it instead of using the Windows store. */
export async function localCaFingerprint(): Promise<string | undefined> {
  try {
    const response = await fetch('/api/auth/session', {
      credentials: 'same-origin',
      cache: 'no-store',
    });
    const data = parse(await response.json());
    return typeof data.ca_sha256 === 'string' && /^[0-9a-f]{64}$/.test(data.ca_sha256)
      ? data.ca_sha256
      : undefined;
  } catch {
    return undefined;
  }
}
