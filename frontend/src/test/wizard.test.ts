import { describe, expect, it } from 'vitest';
import { deviceSchema, type Device } from '../api/contracts';
import {
  computeLevels,
  computeSteps,
  hostSchema,
  originProblem,
  transportMode,
  type HostInfo,
  type WizardInput,
} from '../wizard/model';
import { buildReport, scrub } from '../wizard/report';

const t = (en: string) => en;
function fail(id: string): never {
  throw Error('Missing step ' + id);
}
const host = (extra: Record<string, unknown> = {}): HostInfo =>
  hostSchema.parse({
    serial: [],
    bluetooth: [],
    volumes: [],
    usb: [],
    diagnostics: [],
    environment: { bridge_version: '0.2.0', os: 'Windows 10', python_gammu: true },
    ...extra,
  });
const phone = (extra: Record<string, unknown> = {}): Device =>
  deviceSchema.parse({
    id: 'usb',
    name: 'Nokia E52',
    model: 'Nokia E52 (RM-469)',
    rm: 'RM-469',
    firmware: '071.004',
    transport: 'gammu',
    connection_kind: 'gammu-usb',
    connected: true,
    capabilities: [],
    capabilities_checked_at: '2026-11-04T10:00:00+00:00',
    capability_matrix: {
      'identity.read': {
        mode: 'read',
        status: 'available',
        attempt: 'verified',
        source: 'gammu-native:details',
        evidence: 'private text',
        checked_at: '2026-11-04T10:00:00+00:00',
      },
    },
    ...extra,
  });
const base = (patch: Partial<WizardInput> = {}): WizardInput => ({
  origin: { protocol: 'https:', hostname: 'symport.example' },
  symport: 'ok',
  connection: { mode: 'windows-bridge', connected: true, expires_in: 3600 },
  probe: { kind: 'ok', version: '0.2.0', pairAvailable: true },
  pair: { kind: 'ok', version: '0.2.0' },
  host: host({ serial: [{ port: 'COM3' }] }),
  devices: [phone()],
  rows: [],
  selectedId: 'usb',
  lostIdentity: false,
  t,
  ...patch,
});
const step = (input: WizardInput, id: string) =>
  computeSteps(input).find((s) => s.id === id) ?? fail(id);

describe('first-connection wizard steps', () => {
  it('walks through every step on the happy path', () => {
    const steps = computeSteps(base());
    expect(steps.map((s) => s.status)).toEqual([
      'success',
      'success',
      'success',
      'success',
      'success',
      'success',
      'success',
      'success',
    ]);
    expect(steps[5]?.summary).toContain('RM-469');
    expect(steps[7]?.summary).toContain('identity.read');
  });

  it('local transports skip Bridge and pairing but still check the phone', () => {
    const steps = computeSteps(
      base({ connection: { mode: 'direct', connected: true }, probe: null, pair: null }),
    );
    expect(steps[1]?.status).toBe('skipped');
    expect(steps[2]?.status).toBe('skipped');
    expect(steps[5]?.status).toBe('success');
  });

  it('explains a Bridge that is not running or blocked, with concrete actions', () => {
    const s = step(
      base({
        connection: { mode: 'windows-bridge', connected: false },
        probe: { kind: 'unreachable' },
        pair: null,
      }),
      'bridge',
    );
    expect(s.status).toBe('failed');
    expect(s.code).toBe('bridge-not-found');
    expect(s.fixes.join(' ')).toMatch(/SymPortBridge\.exe/);
    expect(s.fixes.join(' ')).toMatch(/firewall/i);
  });

  it('separates a rejected origin from an unreachable Bridge', () => {
    const s = step(
      base({
        connection: { mode: 'windows-bridge', connected: false },
        probe: { kind: 'origin' },
        pair: null,
      }),
      'bridge',
    );
    expect(s.code).toBe('origin-rejected');
  });

  it('flags plain HTTP on a remote address before trying anything', () => {
    const input = base({
      origin: { protocol: 'http:', hostname: '192.168.1.5' },
      connection: { mode: 'windows-bridge', connected: false },
      probe: null,
      pair: null,
    });
    expect(originProblem(input.origin)).toBe(true);
    expect(originProblem({ protocol: 'http:', hostname: 'localhost' })).toBe(false);
    expect(step(input, 'bridge').code).toBe('origin-not-secure');
  });

  it("explains why an approved Bridge cannot connect, using the Bridge's own report", () => {
    const input = base({
      connection: { mode: 'windows-bridge', connected: false },
      probe: { kind: 'ok', version: '0.3.5', pairAvailable: true },
      pair: { kind: 'ok', version: '0.3.5' },
      grace: true,
      bridgeRelay: { state: 'error', reason: 'tls-untrusted' },
    });
    const pairing = step(input, 'pairing');
    expect(pairing.status).toBe('failed');
    expect(pairing.code).toBe('relay-tls-untrusted');
    expect(pairing.fixes.length).toBeGreaterThan(0);
    for (const reason of [
      'tls-hostname',
      'tls-expired',
      'lease-expired',
      'lease-missing',
      'bad-token',
    ]) {
      const found = step({ ...input, bridgeRelay: { state: 'error', reason } }, 'pairing');
      expect(found.code).toBe(`relay-${reason}`);
      expect(found.fixes.length).toBeGreaterThan(0);
    }
    const waiting = step(
      { ...input, bridgeRelay: { state: 'connecting', reason: null } },
      'pairing',
    );
    expect(waiting.status).toBe('loading');
  });

  it.each([
    ['declined', 'pair-declined'],
    ['timeout', 'pair-timeout'],
    ['wait', 'pair-wait'],
    ['busy', 'pair-busy'],
    ['unreachable', 'pair-unreachable'],
    ['invalid', 'pair-invalid'],
  ] as const)('pairing %s is its own failure with a fix', (kind, code) => {
    const s = step(
      base({ connection: { mode: 'windows-bridge', connected: false }, pair: { kind } }),
      'pairing',
    );
    expect(s.status).toBe('failed');
    expect(s.code).toBe(code);
    expect(s.fixes.length).toBeGreaterThan(0);
  });

  it('shows an open approval dialog as in progress, not as failure', () => {
    expect(
      step(
        base({ connection: { mode: 'windows-bridge', connected: false }, pair: 'loading' }),
        'pairing',
      ).status,
    ).toBe('loading');
  });

  it('gives the relay a grace period after approval, then reports a lost Bridge', () => {
    const waiting = base({
      connection: { mode: 'windows-bridge', connected: false },
      pair: { kind: 'ok', version: null },
      grace: true,
    });
    expect(step(waiting, 'pairing').status).toBe('loading');
    const lost = { ...waiting, grace: false };
    expect(step(lost, 'pairing').code).toBe('bridge-lost');
  });

  it('distinguishes a charge-only cable, a wrong USB mode and a missing driver', () => {
    const none = step(base({ host: host({ diagnostics: ['x'] }) }), 'usb');
    expect(none.code).toBe('no-usb-device');
    expect(none.fixes.join(' ')).toMatch(/charge-only/);
    const mode = step(
      base({ host: host({ usb: [{ vid: '0421', pid: '01a2', class: 'USB', status: 'ok' }] }) }),
      'usb',
    );
    expect(mode.code).toBe('wrong-usb-mode');
    expect(mode.fixes.join(' ')).toMatch(/PC Suite/);
    // Real report: Nokia E72 on Windows 11 is one WPD (MTP) device — no COM port, no drive letter.
    const mtp = step(
      base({ host: host({ usb: [{ vid: '0421', pid: '0223', class: 'WPD', status: 'ok' }] }) }),
      'usb',
    );
    expect(mtp.code).toBe('mtp-only');
    expect(mtp.fixes.join(' ')).toMatch(/Mass Storage/);
    expect(mtp.fixes.join(' ')).toMatch(/driver/);
    const driver = step(
      base({
        host: host({ usb: [{ vid: '0421', pid: '01a2', class: 'USB', status: 'problem' }] }),
      }),
      'usb',
    );
    expect(driver.code).toBe('driver-missing');
    expect(driver.fixes.join(' ')).toMatch(/does not install/);
  });

  it('does not call a readable MTP phone a dead end, and never a phone identity', () => {
    const input = base({
      devices: [],
      host: host({
        usb: [{ vid: '0421', pid: '0223', class: 'WPD', status: 'ok' }],
        portable: [{ index: 0, storages: ['Phone memory', 'Memory card'] }],
      }),
    });
    const usb = step(input, 'usb');
    expect(usb.status).toBe('success');
    expect(usb.summary).toMatch(/MTP/);
    expect(usb.summary).toMatch(/not PC Suite/);
    const driver = step(input, 'driver');
    expect(driver.status).toBe('unsupported');
    expect(driver.summary).toMatch(/Contacts, calendar and SMS/);
    const files = deviceSchema.parse({
      id: 'wpd-nokia',
      transport: 'wpd',
      connected: true,
      model: null,
      capabilities: [],
    });
    expect(transportMode(files)).toBe('mtp');
    expect(step({ ...input, devices: [files] }, 'identify').status).not.toBe('success');
    // WPD without readable storage keeps the cautionary driver/mode advice
    const locked = step(
      base({
        devices: [],
        host: host({
          usb: [{ vid: '0421', pid: '0223', class: 'WPD', status: 'ok' }],
          portable: [{ index: 0, storages: [] }],
        }),
      }),
      'usb',
    );
    expect(locked.code).toBe('mtp-only');
  });

  it('keeps USB candidates apart from a confirmed phone', () => {
    const input = base({ devices: [], host: host({ serial: [{ port: 'COM3' }] }) });
    expect(step(input, 'usb').summary).toMatch(/Candidates only/);
    expect(step(input, 'identify').status).toBe('pending');
    const levels = computeLevels(input);
    expect(levels.usb).toBe(true);
    expect(levels.identified).toBe(false);
  });

  it('reports a COM port held by Nokia Suite', () => {
    const s = step(
      base({
        devices: [phone({ connected: false, model: null, failure: { reason: 'port-busy' } })],
      }),
      'driver',
    );
    expect(s.status).toBe('failed');
    expect(s.code).toBe('port-busy');
    expect(s.fixes.join(' ')).toMatch(/Nokia Suite/);
  });

  it('reports a configured connection whose phone does not answer', () => {
    const s = step(
      base({
        devices: [phone({ connected: false, model: null, failure: { reason: 'no-response' } })],
      }),
      'identify',
    );
    expect(s.status).toBe('failed');
    expect(s.code).toBe('no-response');
    expect(s.fixes.join(' ')).toMatch(/PC Suite/);
  });

  it('reports a phone that disappeared after it had been identified', () => {
    const s = step(
      base({ devices: [phone({ connected: false })], lostIdentity: true }),
      'identify',
    );
    expect(s.code).toBe('device-lost');
    expect(s.summary).toMatch(/disconnected/);
  });

  it('treats a non-Nokia/unsupported answer as unsupported, not as failure', () => {
    const s = step(
      base({
        devices: [
          phone({ connected: false, model: null, failure: { reason: 'unsupported-model' } }),
        ],
      }),
      'identify',
    );
    expect(s.status).toBe('unsupported');
  });

  it('Mass Storage alone is a files-only mode, never a phone identity', () => {
    const volume = deviceSchema.parse({
      id: 'card',
      name: 'Card',
      transport: 'mounted-volume',
      connected: true,
      capabilities: [],
    });
    const input = base({ devices: [volume], host: host({ volumes: [{}] }), selectedId: 'card' });
    expect(step(input, 'driver').status).toBe('unsupported');
    expect(step(input, 'identify').status).not.toBe('success');
    expect(step(input, 'transport').summary).toMatch(/Mass Storage/);
    expect(transportMode(volume)).toBe('mass-storage');
    expect(computeLevels(input).identified).toBe(false);
  });

  it('classifies transports from what the connection reports', () => {
    const mode = (extra: Record<string, unknown>) => transportMode(phone(extra));
    expect(mode({ connection_kind: 'gammu-usb' })).toBe('pc-suite');
    expect(mode({ connection_kind: 'gammu-bluetooth' })).toBe('bluetooth');
    expect(mode({ transport: 'obexftp', connection_kind: 'usb' })).toBe('obex-usb');
    expect(mode({ transport: 'obex-opp', connection_kind: 'bluetooth' })).toBe('obex-opp');
    expect(mode({ connection_kind: null })).toBe('gammu');
  });

  it('refuses to call anything available when the component sent no probe results', () => {
    const s = step(base({ devices: [phone({ capability_matrix: undefined })] }), 'capabilities');
    expect(s.status).toBe('failed');
    expect(s.code).toBe('no-matrix');
  });

  it('keeps the five levels apart', () => {
    const levels = computeLevels(base({ devices: [phone()] }));
    expect(levels).toEqual({
      bridge: true,
      usb: true,
      identified: true,
      service: true,
      operation: false,
    });
    const verified = phone({
      capability_matrix: {
        'files.upload': {
          mode: 'write',
          status: 'available',
          attempt: 'verified',
          source: 'observed-operation',
          evidence: '',
          checked_at: '2026-11-04T10:00:00+00:00',
        },
      },
    });
    expect(computeLevels(base({ devices: [verified] })).operation).toBe(true);
    expect(
      computeLevels(base({ connection: { mode: 'windows-bridge', connected: false } })).bridge,
    ).toBe(false);
  });

  it('shows loading and error states for SymPort and the phone list', () => {
    expect(step(base({ symport: 'loading' }), 'symport').status).toBe('loading');
    expect(step(base({ symport: 'error' }), 'symport').code).toBe('symport-unreachable');
    expect(step(base({ devices: 'loading' }), 'identify').status).toBe('loading');
    expect(step(base({ devices: 'error' }), 'identify').code).toBe('discover-failed');
    expect(step(base({ host: 'error' }), 'usb').code).toBe('host-failed');
  });
});

describe('de-identified report', () => {
  const hostile = phone({
    model: 'Nokia E52 IMEI 356938035643809 mac AA:BB:CC:DD:EE:FF',
    firmware: '071.004 C:\\Users\\alice\\secret.txt +49 170 1234567',
    imei: '356938035643809',
    capability_matrix: {
      'contacts.read': {
        mode: 'read',
        status: 'available',
        attempt: 'verified',
        source: 'gammu-native:GetMemoryStatus',
        evidence: 'John Doe +49 170 1234567 contacts.vcf',
        checked_at: '2026-11-04T10:00:00+00:00',
      },
    },
  });
  const input = base({
    devices: [hostile],
    host: host({
      serial: [{ port: 'COM7' }],
      bluetooth: [{ address: 'AA:BB:CC:DD:EE:FF', name: 'Alice phone' }],
      volumes: [{ root: 'E:/' }],
    }),
  });
  const text = JSON.stringify(buildReport(input, new Date('2026-11-04T10:00:00Z')));

  it('never contains identifiers, paths, evidence text or tokens', () => {
    for (const secret of [
      '356938035643809',
      'AA:BB:CC:DD:EE:FF',
      'alice',
      'Alice',
      'secret.txt',
      '1234567',
      'John',
      'contacts.vcf',
      'COM7',
      'E:/',
      'symport.example',
    ]) {
      expect(text).not.toContain(secret);
    }
  });

  it('keeps what a first E52 test needs', () => {
    const report = buildReport(input, new Date('2026-11-04T10:00:00Z'));
    expect(report.schema).toBe('symport.first-connection-report/1');
    expect(report.hardware_verified).toBe(false);
    expect(report.bridge).toMatchObject({
      version: '0.2.0',
      host_os: 'Windows 10',
      probe: 'ok',
      pairing: 'ok',
    });
    expect(report.host).toMatchObject({ com_ports: 1, drives: 1, bluetooth_records: 1 });
    expect(report.stage).toBe('complete');
    expect(report.connection?.capabilities['contacts.read']).toEqual({
      status: 'available',
      attempt: 'verified',
      source: 'gammu-native:GetMemoryStatus',
    });
    expect(report.steps).toHaveLength(8);
  });

  it('names the failing stage and error code', () => {
    const failed = buildReport(
      base({
        devices: [phone({ connected: false, model: null, failure: { reason: 'no-response' } })],
      }),
    );
    expect(failed.stage).toBe('identify');
    expect(failed.error).toBe('no-response');
    expect(failed.connection).toBeNull();
  });

  it('scrubs identifiers from free text', () => {
    expect(scrub('IMEI 356938035643809')).toBe('IMEI [number]');
    expect(scrub('00:11:22:33:44:55')).toBe('[mac]');
    expect(scrub('/home/alice/x')).toBe('[path]');
    expect(scrub(null)).toBeNull();
  });
});
