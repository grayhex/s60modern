import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from '../api/client';
import { ApiProvider, PreferencesProvider } from '../state/app';
import type { Device } from '../api/contracts';
import Personal from '../features/Personal';
import RouteChips from '../components/RouteChips';
import { recommended, routeBanner, routeOf, stateOf } from '../wizard/routes';
import { transportMode } from '../wizard/model';

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

const matrix = (status: 'available' | 'error' | 'unsupported') => ({
  'contacts.read': {
    mode: 'read' as const,
    status,
    attempt: status === 'available' ? ('verified' as const) : ('blocked' as const),
    source: 'x',
    evidence: 'x',
    checked_at: 'x',
  },
});
const base = { name: null, connected: true, capabilities: [] as string[], transport: 'gammu' };
const pccs = (extra: Partial<Device> = {}): Device =>
  ({
    ...base,
    id: 'nokia-pccs',
    transport: 'pccs',
    name: 'Nokia E72',
    route: 'pccs',
    route_state: 'available',
    recommended_for: ['contacts', 'calendar'],
    dead_end: true,
    capability_matrix: {
      ...matrix('available'),
      'calendar.read': matrix('available')['contacts.read'],
    },
    ...extra,
  }) as Device;
const gammu = (extra: Partial<Device> = {}): Device =>
  ({
    ...base,
    id: 'gammu',
    route: 'gammu',
    route_state: 'silent',
    connected: false,
    capability_matrix: matrix('error'),
    ...extra,
  }) as Device;
const volume = (): Device =>
  ({ ...base, id: 'auto-card-e', transport: 'mounted-volume', capability_matrix: {} }) as Device;

describe('route helpers', () => {
  it('classifies rows with and without the Bridge route fields', () => {
    expect(routeOf(pccs())).toBe('pccs');
    expect(routeOf({ route: null, transport: 'wpd' })).toBe('mtp');
    expect(routeOf({ route: undefined, transport: 'mounted-volume' })).toBe('mass-storage');
    expect(routeOf({ route: 'bogus', transport: 'obexftp' })).toBeNull();
    expect(stateOf({ route_state: undefined, connected: true })).toBe('available');
    expect(stateOf({ route_state: undefined, connected: false })).toBe('unavailable');
    expect(stateOf({ route_state: 'busy', connected: false })).toBe('busy');
    expect(recommended(pccs())).toBe(true);
    expect(recommended(gammu())).toBe(false);
    expect(transportMode(pccs())).toBe('usb-obex');
  });

  it.each([
    ['nothing selected, Gammu silent', [pccs(), gammu()], '', 'contacts', 'switch'],
    ['a drive is selected', [pccs(), volume()], 'auto-card-e', 'calendar', 'switch'],
    ['USB OBEX in use, Gammu offered', [pccs(), gammu()], 'nokia-pccs', 'contacts', 'using'],
    [
      'Gammu selected and able to read',
      [
        pccs(),
        gammu({ connected: true, route_state: 'verified', capability_matrix: matrix('available') }),
      ],
      'gammu',
      'contacts',
      null,
    ],
    ['SMS never takes the banner', [pccs(), gammu()], '', 'sms', null],
    [
      'not recommended for this store',
      [pccs({ recommended_for: ['calendar'] })],
      '',
      'contacts',
      null,
    ],
    ['USB OBEX not connected', [pccs({ connected: false })], '', 'contacts', null],
    ['no USB OBEX row at all', [gammu()], '', 'contacts', null],
  ] as const)('%s', (_name, devices, selected, domain, kind) => {
    expect(routeBanner([...devices], selected, domain)?.kind ?? null).toBe(kind);
  });

  it('tells apart a Gammu dead end from a plain suggestion, and offers Gammu back only when it exists', () => {
    expect(routeBanner([pccs()], '', 'contacts')).toMatchObject({ kind: 'switch', deadEnd: true });
    expect(routeBanner([pccs({ dead_end: false })], '', 'contacts')).toMatchObject({
      deadEnd: false,
    });
    expect(routeBanner([pccs()], 'nokia-pccs', 'contacts')).toMatchObject({
      kind: 'using',
      back: null,
    });
    expect(routeBanner([pccs(), gammu()], 'nokia-pccs', 'contacts')).toMatchObject({
      back: { id: 'gammu' },
    });
  });
});

function renderWith(node: React.ReactNode, language: 'en' | 'ru') {
  localStorage.setItem('symport-language', language);
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={cache}>
      <ApiProvider client={new ApiClient(() => 'test')}>
        <PreferencesProvider>{node}</PreferencesProvider>
      </ApiProvider>
    </QueryClientProvider>,
  );
}

describe('route chips', () => {
  it.each([
    ['en', 'Recommended', 'USB OBEX (Nokia PCCS) · available'],
    ['ru', 'Рекомендуется', 'USB OBEX (Nokia PCCS) · доступен'],
  ] as const)('shows the state and “Recommended” in %s', (language, word, label) => {
    renderWith(<RouteChips device={pccs()} />, language);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`^${word}:`))).toBeInTheDocument();
  });
  it('shows no recommendation for a route the Bridge does not recommend', () => {
    renderWith(<RouteChips device={gammu()} />, 'en');
    expect(screen.getByText('Gammu (PC Suite) · found, phone silent')).toBeInTheDocument();
    expect(screen.queryByText(/Recommended/)).not.toBeInTheDocument();
  });
});

describe('route banner on the contacts page', () => {
  function stub(devices: Device[]) {
    const actions: { action: string; device?: string }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        const body = JSON.parse(String(init.body)) as { action: string; device?: string };
        actions.push(body);
        if (body.action === 'discover')
          return new Response(JSON.stringify({ devices, limitations: {} }));
        if (body.action === 'connection-select')
          return new Response(JSON.stringify({ status: 'selected', device: body.device }));
        return new Response(JSON.stringify({}));
      }),
    );
    return actions;
  }

  it('offers the switch when Gammu is a dead end, journals the choice and selects USB OBEX', async () => {
    const actions = stub([pccs(), gammu()]);
    renderWith(<Personal domain="contacts" />, 'ru');
    await screen.findByText(
      'Gammu не достучался до телефона, контакты и календарь доступны по USB OBEX.',
    );
    await userEvent.click(screen.getByRole('button', { name: 'Переключиться' }));
    await waitFor(() =>
      expect(actions).toContainEqual({ action: 'connection-select', device: 'nokia-pccs' }),
    );
    // after the switch the page reports that USB OBEX is in use and offers the way back to Gammu
    await screen.findByText(/Используется USB OBEX/);
    expect(screen.getByRole('button', { name: 'Вернуться к Gammu' })).toBeInTheDocument();
    expect(actions.some((a) => /read|write|apply|import/.test(a.action))).toBe(false);
  });

  it('goes back to Gammu on request and shows no banner when USB OBEX is not recommended', async () => {
    stub([
      pccs(),
      gammu({ connected: true, route_state: 'verified', capability_matrix: matrix('available') }),
    ]);
    renderWith(<Personal domain="contacts" />, 'en');
    await screen.findByRole('option', { name: /Nokia E72/ });
    await userEvent.selectOptions(
      screen.getByLabelText('Connection', { exact: true }),
      'nokia-pccs',
    );
    await userEvent.click(await screen.findByRole('button', { name: 'Return to Gammu' }));
    await waitFor(() => expect(screen.queryByText(/Using USB OBEX/)).not.toBeInTheDocument());
  });

  it('shows nothing without a USB OBEX row', async () => {
    stub([gammu({ connected: true, route_state: 'verified' })]);
    renderWith(<Personal domain="contacts" />, 'en');
    await screen.findByLabelText('Connection', { exact: true });
    expect(screen.queryByText(/USB OBEX/)).not.toBeInTheDocument();
  });
});
