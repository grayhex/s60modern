import type { Device } from '../api/contracts';

/**
 * Routes to a Nokia, as the Windows Bridge reports them on every connection row (`route`, `route_state`,
 * `recommended_for`). The state is the Bridge's own; this module only names it and decides what to suggest.
 */
export type RouteId = 'gammu' | 'pccs' | 'mtp' | 'mass-storage';
export type RouteJob = 'contacts' | 'calendar' | 'sms' | 'files';
export const ROUTE_TEXT: Record<RouteId, [string, string]> = {
  gammu: ['Gammu (PC Suite)', 'Gammu (PC Suite)'],
  pccs: ['USB OBEX (Nokia PCCS)', 'USB OBEX (Nokia PCCS)'],
  mtp: ['MTP (files, read-only)', 'MTP (файлы, только чтение)'],
  'mass-storage': ['Mass storage', 'Mass storage'],
};
export const STATE_TEXT: Record<string, [string, string]> = {
  unavailable: ['not available', 'недоступен'],
  busy: ['busy', 'занят'],
  silent: ['found, phone silent', 'найден, телефон молчит'],
  available: ['available', 'доступен'],
  verified: ['verified', 'проверен'],
};
export const JOB_TEXT: Record<string, [string, string]> = {
  contacts: ['contacts', 'контакты'],
  calendar: ['calendar', 'календарь'],
  sms: ['SMS', 'SMS'],
  files: ['files', 'файлы'],
};

const ROUTES: readonly string[] = ['gammu', 'pccs', 'mtp', 'mass-storage'];
/** The route of a row; rows from a Bridge that predates routes are classified from their transport. */
export function routeOf(d: Pick<Device, 'route' | 'transport'>): RouteId | null {
  if (d.route && ROUTES.includes(d.route)) return d.route as RouteId;
  if (d.transport === 'mounted-volume') return 'mass-storage';
  if (d.transport === 'wpd') return 'mtp';
  if (d.transport === 'pccs') return 'pccs';
  if (d.transport === 'gammu') return 'gammu';
  return null;
}
/** State shown for a row: the Bridge's, or (older Bridge) derived from whether the connection answered. */
export function stateOf(d: Pick<Device, 'route_state' | 'connected'>): string {
  return d.route_state ?? (d.connected ? 'available' : 'unavailable');
}
export const recommended = (d: Pick<Device, 'recommended_for'>) =>
  (d.recommended_for ?? []).length > 0;

const readOp = (domain: string) => (domain === 'tasks' ? 'tasks.read' : `${domain}.read`);
const canRead = (d: Device, domain: string) => {
  const state = d.capability_matrix?.[readOp(domain)];
  return Boolean(d.connected) && Boolean(state) && state?.attempt !== 'blocked';
};

export type RouteBanner =
  | { kind: 'switch'; target: Device; deadEnd: boolean }
  | { kind: 'using'; target: Device; back: Device | null };
/**
 * A hint on a contacts/calendar page. `switch`: the selected connection cannot read this store but the USB OBEX route can
 * (Gammu dead end when the Bridge says so). `using`: the USB OBEX route is the one in use; Gammu is offered when it exists.
 * Pure: choosing does not send anything to the phone.
 */
export function routeBanner(
  devices: Device[],
  selectedId: string,
  domain: string,
): RouteBanner | null {
  if (domain !== 'contacts' && domain !== 'calendar') return null;
  const target = devices.find(
    (d) => routeOf(d) === 'pccs' && d.connected && (d.recommended_for ?? []).includes(domain),
  );
  if (!target) return null;
  if (selectedId === target.id) {
    return { kind: 'using', target, back: devices.find((d) => routeOf(d) === 'gammu') ?? null };
  }
  const selected = devices.find((d) => d.id === selectedId);
  if (selected && canRead(selected, domain)) return null;
  return { kind: 'switch', target, deadEnd: Boolean(target.dead_end) };
}
