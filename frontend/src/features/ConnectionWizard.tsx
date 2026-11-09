import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CircleCheck, CircleDashed, CircleX, Ban, Loader, Minus } from 'lucide-react';
import { useApi, usePreferences } from '../state/app';
import { keys, useDevices } from '../api/queries';
import {
  bridgeRelayState,
  localCaFingerprint,
  pairBridge,
  probeBridge,
  type PairResult,
  type ProbeResult,
  type RelayState,
} from '../api/bridge';
import CapabilityMatrix from '../components/CapabilityMatrix';
import { Card, ErrorMessage } from '../components/ui';
import { Download } from '../components/workspace';
import {
  MODE_TEXT,
  computeLevels,
  computeSteps,
  hostSchema,
  filesOnly,
  transportMode,
  type HostInfo,
  type Step,
  type StepStatus,
  type WizardInput,
} from '../wizard/model';
import { buildReport } from '../wizard/report';

const ICON: Record<StepStatus, typeof CircleCheck> = {
  success: CircleCheck,
  failed: CircleX,
  loading: Loader,
  pending: CircleDashed,
  unsupported: Ban,
  skipped: Minus,
};
const LABEL: Record<StepStatus, [string, string]> = {
  success: ['Done', 'Готово'],
  failed: ['Failed', 'Не удалось'],
  loading: ['In progress', 'Выполняется'],
  pending: ['Waiting', 'Ожидает'],
  unsupported: ['Not supported', 'Не поддерживается'],
  skipped: ['Not needed', 'Не требуется'],
};

function base64(text: string) {
  const bytes = new TextEncoder().encode(text);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/**
 * Guided first connection of a Nokia E52/E72. Every step is derived from what was actually
 * observed (Bridge probe, relay heartbeat, host records, device answers, capability probes);
 * nothing is mocked and nothing here writes to the phone. Retrying only re-reads.
 */
export default function ConnectionWizard() {
  const api = useApi();
  const cache = useQueryClient();
  const { t, device, setDevice } = usePreferences();
  const devices = useDevices();
  const [probe, setProbe] = useState<ProbeResult | 'loading' | null>(null);
  const [pair, setPair] = useState<PairResult | null>(null);
  const [pairedAt, setPairedAt] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [identified, setIdentified] = useState(false);
  const [bridgeRelay, setBridgeRelay] = useState<RelayState | null>(null);

  const status = useQuery({
    queryKey: ['connection'],
    queryFn: ({ signal }) => api.connection('status', signal),
    refetchInterval: (query) =>
      query.state.data?.mode === 'windows-bridge' && !query.state.data.connected ? 4000 : 20000,
    retry: false,
  });
  const reachable =
    status.data?.mode === 'direct' ||
    (status.data?.mode === 'windows-bridge' && status.data.connected);
  const host = useQuery({
    queryKey: ['connection-host', status.data?.mode ?? null],
    queryFn: ({ signal }) =>
      api.request('/api/manager', { action: 'connection-host' }, hostSchema, signal),
    enabled: Boolean(reachable),
    staleTime: 0,
    retry: false,
  });

  const checkBridge = useMutation({
    mutationFn: () => probeBridge(location.origin),
    onMutate: () => setProbe('loading'),
    onSuccess: setProbe,
  });
  const request = useMutation({
    mutationFn: async () => {
      const lease = await api.connection('enable');
      if (!lease.relay_token) throw Error('Missing bridge lease');
      const result = await pairBridge(
        location.origin,
        lease.relay_token,
        await localCaFingerprint(),
      );
      // A refused or failed pairing must not leave an unclaimed lease behind.
      if (result.kind !== 'ok') await api.connection('disconnect');
      return result;
    },
    onSuccess: async (result) => {
      setPair(result);
      setPairedAt(Date.now());
      if (result.kind === 'ok') {
        cache.removeQueries({ queryKey: keys.devices });
        setDevice('');
        await cache.invalidateQueries({ queryKey: ['connection'] });
      }
    },
    onError: () => setPair({ kind: 'invalid' }),
  });
  const disconnect = useMutation({
    mutationFn: () => api.connection('disconnect'),
    onSuccess: async () => {
      setPair(null);
      setProbe(null);
      setIdentified(false);
      cache.removeQueries({ queryKey: keys.devices });
      await cache.invalidateQueries({ queryKey: ['connection'] });
    },
  });
  const rerun = useMutation({
    // Read-only: status, device answers and host records. No write is ever repeated.
    mutationFn: async () => {
      await Promise.all([
        cache.invalidateQueries({ queryKey: ['connection'] }),
        cache.invalidateQueries({ queryKey: keys.devices }),
        cache.invalidateQueries({ queryKey: ['connection-host'] }),
      ]);
      if (probe && probe !== 'loading') await checkBridge.mutateAsync();
    },
  });

  const phones = devices.data?.devices ?? [];
  const identifiedNow = phones.some((d) => !filesOnly(d) && d.connected && d.model);
  // Remember that a phone answered once; losing it afterwards is reported as a lost device.
  if (identifiedNow && !identified) setIdentified(true);
  const grace = pair?.kind === 'ok' && now - pairedAt < 25000;
  useEffect(() => {
    if (!grace) return;
    const timer = setTimeout(() => setNow(Date.now()), 26000 - (now - pairedAt));
    return () => clearTimeout(timer);
  }, [grace, now, pairedAt]);
  // After pairing, ask the Bridge itself why it is not connected yet (untrusted certificate, firewall, …).
  const relayUp = status.data?.mode === 'windows-bridge' && status.data.connected;
  const pairedOk = pair?.kind === 'ok';
  useEffect(() => {
    if (!pairedOk || relayUp) return;
    let stopped = false;
    const ask = async () => {
      const state = await bridgeRelayState(location.origin);
      if (!stopped) setBridgeRelay(state);
    };
    void ask();
    const timer = setInterval(() => void ask(), 3000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [pairedOk, relayUp]);
  const hostValue: HostInfo | 'loading' | 'error' | null = !reachable
    ? null
    : host.isPending
      ? 'loading'
      : host.isError
        ? 'error'
        : (host.data ?? null);
  const input: WizardInput = {
    origin: { protocol: location.protocol, hostname: location.hostname },
    symport: status.isPending ? 'loading' : status.isError ? 'error' : 'ok',
    connection: status.data ?? null,
    probe,
    pair: request.isPending ? 'loading' : pair,
    host: hostValue,
    devices: devices.isPending ? 'loading' : devices.isError && !devices.data ? 'error' : phones,
    rows: devices.data?.connections ?? [],
    selectedId: device,
    lostIdentity: identified && !identifiedNow,
    grace,
    bridgeRelay: pairedOk && !relayUp ? bridgeRelay : null,
    t,
  };
  const steps = computeSteps(input);
  const levels = computeLevels(input);
  const report = buildReport(input);
  const direct = status.data?.mode === 'direct';
  const connected = phones.filter((d) => d.connected);
  const selected = phones.find((d) => d.id === device && d.connected);
  const first = steps.findIndex((s) => s.status === 'failed' || s.status === 'pending');
  const busy = request.isPending || checkBridge.isPending || rerun.isPending;

  // Pick the only identified phone automatically; several connections need an explicit choice.
  const only = connected.filter((d) => !filesOnly(d));
  const autoSelected = useRef('');
  useEffect(() => {
    const [one] = only;
    if (!device && only.length === 1 && one && autoSelected.current !== one.id) {
      autoSelected.current = one.id;
      setDevice(one.id);
    }
  }, [device, only, setDevice]);

  return (
    <Card className="wizard">
      <h2>{t('First connection wizard', 'Мастер первого подключения')}</h2>
      <p>
        {t(
          'Each step below shows what was really observed. A USB candidate is not a confirmed phone; a drive is not a phone; reading never proves writing. The wizard changes nothing on the phone.',
          'Каждый шаг показывает то, что действительно было замечено. USB-кандидат не равен подтверждённому телефону, диск не равен телефону, чтение не доказывает запись. Мастер ничего не меняет на телефоне.',
        )}
      </p>
      <ul className="wizard-levels" aria-label={t('Connection state', 'Состояние подключения')}>
        {(
          [
            ['bridge', 'Bridge connected to SymPort', 'Bridge подключён к SymPort'],
            ['usb', 'USB device seen', 'USB-устройство найдено'],
            ['identified', 'Nokia identified', 'Nokia идентифицирована'],
            ['service', 'Service available', 'Сервис доступен'],
            ['operation', 'Operation verified', 'Операция проверена'],
          ] as const
        ).map(([id, en, ru]) => (
          <li key={id} data-on={levels[id]} className="chip">
            {levels[id] ? '✓ ' : '○ '}
            {t(en, ru)}
            <span className="sr-only"> — {levels[id] ? t('yes', 'да') : t('no', 'нет')}</span>
          </li>
        ))}
      </ul>
      <div className="form-row">
        <button
          type="button"
          className="btn secondary"
          disabled={busy}
          onClick={() => checkBridge.mutate()}
        >
          {t('Check Bridge', 'Проверить Bridge')}
        </button>
        <button
          type="button"
          className="btn primary"
          disabled={busy}
          onClick={() => request.mutate()}
        >
          {t('Request access', 'Запросить доступ')}
        </button>
        {!direct && (
          <button
            type="button"
            className="btn ghost"
            disabled={disconnect.isPending}
            onClick={() => disconnect.mutate()}
          >
            {t('Disconnect Bridge', 'Отключить Bridge')}
          </button>
        )}
        <button
          type="button"
          className="btn secondary"
          disabled={busy}
          onClick={() => rerun.mutate()}
        >
          {t('Re-run diagnostics (read-only)', 'Повторить диагностику (только чтение)')}
        </button>
      </div>
      <ErrorMessage error={request.error ?? disconnect.error ?? rerun.error} />
      <ol className="wizard-steps">
        {steps.map((step, index) => (
          <StepItem key={step.id} step={step} number={index + 1} current={index === first} />
        ))}
      </ol>
      {connected.length > 0 && (
        <fieldset className="wizard-choice">
          <legend>{t('Choose the connection to use', 'Выберите подключение')}</legend>
          {connected.map((d) => (
            <label key={d.id}>
              <input
                type="radio"
                name="wizard-connection"
                checked={device === d.id}
                onChange={() => setDevice(d.id)}
              />{' '}
              {d.name ?? d.model ?? d.id} · {t(...MODE_TEXT[transportMode(d)])}
              {filesOnly(d) &&
                ` — ${t('files only, not a phone identity', 'только файлы, не идентичность телефона')}`}
            </label>
          ))}
        </fieldset>
      )}
      {selected && (
        <details>
          <summary>
            {t('Model, firmware and limitations', 'Модель, прошивка и ограничения')}
          </summary>
          <dl className="wizard-facts">
            <dt>{t('Model', 'Модель')}</dt>
            <dd>{selected.model ?? '—'}</dd>
            <dt>RM</dt>
            <dd>{selected.rm ?? '—'}</dd>
            <dt>{t('Firmware', 'Прошивка')}</dt>
            <dd>{selected.firmware ?? '—'}</dd>
            <dt>{t('Transport', 'Транспорт')}</dt>
            <dd>{t(...MODE_TEXT[transportMode(selected)])}</dd>
            <dt>{t('Last checked', 'Проверено')}</dt>
            <dd>{selected.capabilities_checked_at ?? '—'}</dd>
          </dl>
          <p className="note">
            {t(
              'Limits: Mass Storage is the memory card only. A verified read does not prove a write; the first write is confirmed and verified by read-back. SIS copying is not installation — confirm it on the phone.',
              'Ограничения: Mass Storage — только карта памяти. Подтверждённое чтение не доказывает запись; первая запись подтверждается и проверяется обратным чтением. Копирование SIS — не установка: подтвердите её на телефоне.',
            )}
          </p>
          <CapabilityMatrix device={selected} />
          <p>
            <a href="#tools/diagnostics">{t('Open diagnostics', 'Открыть диагностику')}</a>
          </p>
        </details>
      )}
      <div className="form-row">
        <Download
          content={base64(JSON.stringify(report, null, 2))}
          name="symport-first-connection-report.json"
          mime="application/json"
        />
      </div>
      <p className="note">
        {t(
          'The report lists steps, error codes, USB vendor/product ids, counts, model/RM/firmware and capability states. It never contains IMEI, MAC addresses, serial numbers, tokens, contacts, SMS, file names or private paths. Review it before sharing.',
          'Отчёт содержит шаги, коды ошибок, USB vendor/product id, количества, модель/RM/прошивку и состояния возможностей. В нём нет IMEI, MAC-адресов, серийных номеров, токенов, контактов, SMS, имён файлов и личных путей. Проверьте его перед отправкой.',
        )}
      </p>
    </Card>
  );
}

function StepItem({ step, number, current }: { step: Step; number: number; current: boolean }) {
  const { t } = usePreferences();
  const Icon = ICON[step.status];
  return (
    <li
      className="wizard-step"
      data-step={step.id}
      data-status={step.status}
      aria-current={current ? 'step' : undefined}
    >
      <Icon
        className={`wizard-icon ${step.status === 'loading' ? 'spin' : ''}`}
        aria-hidden="true"
        size={20}
      />
      <div>
        <strong>
          {number}. {step.title}
        </strong>{' '}
        <span className={`chip wizard-state wizard-state--${step.status}`}>
          {t(...LABEL[step.status])}
        </span>
        <p role="status">{step.summary}</p>
        {step.fixes.length > 0 && step.status !== 'success' && (
          <ul className="wizard-fixes">
            {step.fixes.map((fix) => (
              <li key={fix}>{fix}</li>
            ))}
          </ul>
        )}
        {step.code && (
          <small className="capability-source">
            {t('Code', 'Код')}: {step.code}
          </small>
        )}
      </div>
    </li>
  );
}
