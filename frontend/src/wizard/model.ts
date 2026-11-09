import { z } from 'zod';
import type { ConnectionRow, Device } from '../api/contracts';
import type { PairResult, ProbeResult } from '../api/bridge';

export const hostSchema = z.object({
  serial: z.array(z.object({ port: z.string().optional() }).passthrough()).default([]),
  bluetooth: z.array(z.object({}).passthrough()).default([]),
  volumes: z.array(z.object({}).passthrough()).default([]),
  usb: z
    .array(
      z.object({
        vid: z.string(),
        pid: z.string(),
        class: z.string().optional(),
        status: z.enum(['ok', 'problem']),
      }),
    )
    .default([]),
  portable: z
    .array(z.object({ index: z.number().optional(), storages: z.array(z.string()).default([]) }))
    .default([]),
  diagnostics: z.array(z.string()).default([]),
  environment: z
    .object({
      bridge_version: z.string().nullish(),
      os: z.string().nullish(),
      python_gammu: z.boolean().nullish(),
    })
    .optional(),
});
export type HostInfo = z.infer<typeof hostSchema>;

export type StepId =
  'symport' | 'bridge' | 'pairing' | 'usb' | 'driver' | 'identify' | 'transport' | 'capabilities';
export type StepStatus = 'pending' | 'loading' | 'success' | 'failed' | 'unsupported' | 'skipped';
export type Step = {
  id: StepId;
  status: StepStatus;
  title: string;
  summary: string;
  /** Stable machine code of the failure; also used in the exported report. */
  code: string | null;
  fixes: string[];
};
export type Translate = (en: string, ru: string) => string;

export type WizardInput = {
  origin: { protocol: string; hostname: string };
  symport: 'loading' | 'ok' | 'error';
  connection: { mode: string; connected: boolean; expires_in?: number | undefined } | null;
  probe: ProbeResult | 'loading' | null;
  pair: PairResult | 'loading' | null;
  host: HostInfo | 'loading' | 'error' | null;
  devices: Device[] | 'loading' | 'error';
  rows: ConnectionRow[];
  selectedId: string;
  /** A connection that was identified earlier in this session is gone or no longer answers. */
  lostIdentity: boolean;
  /** Pairing just succeeded; the relay needs a few seconds for its first heartbeat. */
  grace?: boolean;
  /** The paired Bridge's own report about its link to SymPort. */
  bridgeRelay?: { state: string; reason: string | null } | null;
  t: Translate;
};

/** Why a paired Bridge cannot reach SymPort, as reported by the Bridge itself. */
function relayFailure(reason: string | null, title: [string, string], make: Maker): Step {
  const fixes: Record<string, [string, string][]> = {
    'tls-untrusted': [
      [
        "The PC does not trust the SymPort certificate. Request access again: the Bridge pins SymPort's own certificate when you approve it (SymPort 0.10+ with Home LAN HTTPS).",
        'ПК не доверяет сертификату SymPort. Запросите доступ ещё раз: Bridge запоминает собственный сертификат SymPort при подтверждении (SymPort 0.10+ с HTTPS Home LAN).',
      ],
      [
        'Or install the certificate from the SymPort login page into Trusted Root Certification Authorities (Current User) and restart Bridge.',
        'Либо установите сертификат со страницы входа SymPort в «Доверенные корневые центры сертификации» (текущий пользователь) и перезапустите Bridge.',
      ],
    ],
    'tls-error': [
      [
        'The secure connection to SymPort failed. Open SymPort with https:// and update Bridge to 0.3.4 or newer.',
        'Не удалось установить защищённое соединение с SymPort. Откройте SymPort по https:// и обновите Bridge до 0.3.4 или новее.',
      ],
    ],
    unreachable: [
      [
        'The PC cannot reach SymPort at this address and port. Check the network, the NAS address and the Windows firewall.',
        'ПК не может подключиться к SymPort по этому адресу и порту. Проверьте сеть, адрес NAS и брандмауэр Windows.',
      ],
    ],
    'tls-hostname': [
      [
        'The SymPort certificate was issued for another address. Restart SymPort (it reissues the certificate for its current address) and request access again from the address in its log.',
        'Сертификат SymPort выпущен на другой адрес. Перезапустите SymPort (он выпустит сертификат на текущий адрес) и запросите доступ заново с адреса из его журнала.',
      ],
    ],
    'tls-expired': [
      [
        'The SymPort certificate has expired. Restart SymPort to renew it and check the date and time on the PC and the NAS.',
        'Срок сертификата SymPort истёк. Перезапустите SymPort, чтобы он обновился, и проверьте дату и время на ПК и NAS.',
      ],
    ],
    'lease-expired': [
      [
        'The access lease expired or was revoked. Request access again (it lasts two hours and ends when SymPort restarts).',
        'Срок доступа истёк или он отозван. Запросите доступ снова (он действует два часа и сбрасывается при перезапуске SymPort).',
      ],
    ],
    'lease-missing': [
      [
        'SymPort has no lease for this Bridge (it was restarted or the lease was closed). Request access again.',
        'У SymPort нет доступа для этого Bridge (он перезапущен или доступ закрыт). Запросите доступ снова.',
      ],
    ],
    'bad-token': [
      [
        'SymPort rejected the token: a newer request replaced this one. Request access once more and approve only the latest dialog.',
        'SymPort отклонил токен: его заменил более новый запрос. Запросите доступ ещё раз и подтверждайте только последнее окно.',
      ],
    ],
    rejected: [
      [
        'SymPort refused the access lease. Request access again.',
        'SymPort отклонил выданный доступ. Запросите доступ ещё раз.',
      ],
    ],
  };
  return make(
    'pairing',
    title,
    'failed',
    [
      'Approved, but the Bridge cannot connect to SymPort.',
      'Доступ разрешён, но Bridge не может подключиться к SymPort.',
    ],
    `relay-${reason ?? 'error'}`,
    fixes[reason ?? ''] ?? [
      [
        'Open Bridge → Diagnostics in the tray for details.',
        'Откройте в трее Bridge → Diagnostics.',
      ],
    ],
  );
}

const isLocal = (hostname: string) => ['localhost', '127.0.0.1', '[::1]', '::1'].includes(hostname);
export const originProblem = (o: WizardInput['origin']) =>
  o.protocol !== 'https:' && !isLocal(o.hostname);

export type Mode =
  | 'pc-suite'
  | 'gammu'
  | 'usb-obex'
  | 'mass-storage'
  | 'mtp'
  | 'bluetooth'
  | 'obex-usb'
  | 'obex-opp';
/** Connections that expose files only and never identify a phone (a drive letter, or Windows MTP). */
export const filesOnly = (d: { transport?: string | null | undefined }) =>
  d.transport === 'mounted-volume' || d.transport === 'wpd';
/** The kind of link, from what the connection reports; never from a drive letter or a name. */
export function transportMode(d: Device): Mode {
  if (d.transport === 'mounted-volume') return 'mass-storage';
  if (d.transport === 'wpd') return 'mtp';
  if (d.transport === 'pccs') return 'usb-obex';
  if (d.transport === 'obex-opp') return 'obex-opp';
  if (d.transport === 'obexftp') return d.connection_kind === 'usb' ? 'obex-usb' : 'bluetooth';
  if (d.connection_kind === 'gammu-bluetooth' || d.connection_kind === 'gammu-s60')
    return 'bluetooth';
  if (d.connection_kind === 'gammu-usb') return 'pc-suite';
  return 'gammu';
}
export const MODE_TEXT: Record<Mode, [string, string]> = {
  'pc-suite': ['PC Suite (USB, Gammu)', 'PC Suite (USB, Gammu)'],
  gammu: ['Gammu connection', 'Подключение Gammu'],
  'usb-obex': [
    'Nokia USB OBEX (contacts and calendar, read-only)',
    'Nokia USB OBEX (контакты и календарь, только чтение)',
  ],
  'mass-storage': ['Mass Storage (memory card files only)', 'Mass Storage (только файлы карты)'],
  mtp: ['Windows MTP (files, read-only)', 'Windows MTP (файлы, только чтение)'],
  bluetooth: ['Bluetooth', 'Bluetooth'],
  'obex-usb': ['OBEX FTP over USB', 'OBEX FTP по USB'],
  'obex-opp': ['Bluetooth OPP (send only)', 'Bluetooth OPP (только отправка)'],
};

const phoneRows = (devices: Device[]) => devices.filter((d) => !filesOnly(d));
const PORT_REASONS = [
  'port-busy',
  'port-open',
  'port-missing',
  'port-permission',
  'driver-missing',
  'gammu-missing',
  'config-invalid',
];

export function computeSteps(input: WizardInput): Step[] {
  const { t } = input;
  const make = (
    id: StepId,
    title: [string, string],
    status: StepStatus,
    summary: [string, string],
    code: string | null = null,
    fixes: [string, string][] = [],
  ): Step => ({
    id,
    status,
    title: t(...title),
    summary: t(...summary),
    code,
    fixes: fixes.map(([en, ru]) => t(en, ru)),
  });
  const waiting: [string, string] = ['Waiting for the previous step', 'Ждёт предыдущий шаг'];
  const direct = input.connection?.mode === 'direct';
  const relayUp = input.connection?.mode === 'windows-bridge' && input.connection.connected;
  const reachable = direct || relayUp;
  const steps: Step[] = [];

  // 1 — SymPort itself
  steps.push(
    input.symport === 'loading'
      ? make('symport', ['SymPort is reachable', 'SymPort доступен'], 'loading', [
          'Checking…',
          'Проверка…',
        ])
      : input.symport === 'error'
        ? make(
            'symport',
            ['SymPort is reachable', 'SymPort доступен'],
            'failed',
            ['The SymPort API does not answer.', 'API SymPort не отвечает.'],
            'symport-unreachable',
            [
              [
                'Reload the page and check that the SymPort server is running.',
                'Перезагрузите страницу и убедитесь, что сервер SymPort запущен.',
              ],
              [
                'Behind a reverse proxy, check that the session token and cookies pass through.',
                'За обратным прокси убедитесь, что токен сессии и cookie проходят.',
              ],
            ],
          )
        : make('symport', ['SymPort is reachable', 'SymPort доступен'], 'success', [
            'The SymPort API answers.',
            'API SymPort отвечает.',
          ]),
  );

  // 2 — Bridge present. Local transports are the default; the Bridge is an alternative the user opts into.
  const bridgeTitle: [string, string] = ['Windows Bridge is running', 'Windows Bridge запущен'];
  const attempted = input.probe !== null || input.pair !== null;
  const probe = input.probe;
  if (input.symport !== 'ok') steps.push(make('bridge', bridgeTitle, 'pending', waiting));
  else if (relayUp)
    steps.push(
      make('bridge', bridgeTitle, 'success', [
        'Bridge is connected to SymPort.',
        'Bridge подключён к SymPort.',
      ]),
    );
  else if (originProblem(input.origin) && (!direct || attempted))
    steps.push(
      make(
        'bridge',
        bridgeTitle,
        'failed',
        [
          'Bridge pairing is impossible from this address.',
          'С этого адреса сопряжение с Bridge невозможно.',
        ],
        'origin-not-secure',
        [
          [
            'Open SymPort over HTTPS: Home LAN mode serves it by default (https://<nas-ip>:8765, SYMPORT_TLS not set to off), or use a trusted reverse proxy with SYMPORT_PUBLIC_ORIGIN, or open it from localhost / an SSH tunnel. Bridge accepts plain HTTP only for localhost.',
            'Откройте SymPort по HTTPS: режим Home LAN отдаёт его так по умолчанию (https://<IP-NAS>:8765, SYMPORT_TLS не равен off), либо через доверенный обратный прокси с SYMPORT_PUBLIC_ORIGIN, либо с localhost / через SSH-туннель. Bridge принимает HTTP только для localhost.',
          ],
          [
            'On a new computer install the SymPort certificate once (link on the login page): double-click it → Install Certificate → Current User → Trusted Root Certification Authorities. The browser then trusts the address (the Windows Bridge pins it itself when you approve the pairing).',
            'На новом компьютере один раз установите сертификат SymPort (ссылка на экране входа): двойной щелчок → «Установить сертификат» → «Текущий пользователь» → «Доверенные корневые центры сертификации». После этого браузер доверяет адресу (Windows Bridge запоминает сертификат сам при подтверждении сопряжения).',
          ],
        ],
      ),
    );
  else if (probe === 'loading')
    steps.push(
      make('bridge', bridgeTitle, 'loading', [
        'Looking for Bridge on this PC…',
        'Ищем Bridge на этом ПК…',
      ]),
    );
  else if (probe === null)
    steps.push(
      direct
        ? make('bridge', bridgeTitle, 'skipped', [
            'Not in use: this SymPort server talks to the phone through its own local transports. If the phone is plugged into a Windows PC instead, press “Check Bridge”.',
            'Не используется: этот сервер SymPort работает с телефоном через свои локальные транспорты. Если телефон подключён к Windows-ПК, нажмите «Проверить Bridge».',
          ])
        : make('bridge', bridgeTitle, 'pending', [
            'Press “Check Bridge”.',
            'Нажмите «Проверить Bridge».',
          ]),
    );
  else if (probe.kind === 'unreachable')
    steps.push(
      make(
        'bridge',
        bridgeTitle,
        'failed',
        ['Bridge was not found on this PC.', 'Bridge на этом ПК не найден.'],
        'bridge-not-found',
        [
          [
            'Start SymPortBridge.exe on the PC the phone is plugged into; its tray icon must be visible.',
            'Запустите SymPortBridge.exe на ПК, к которому подключён телефон; значок должен быть в трее.',
          ],
          [
            'If the browser asked to allow access to local devices/network, allow it and retry.',
            'Если браузер спросил разрешение на доступ к локальной сети, разрешите его и повторите.',
          ],
          [
            'A firewall, security software or a browser policy may block 127.0.0.1:24652; otherwise use the SSH PC Bridge route (docs/DEVICE_MANAGER.md).',
            'Брандмауэр, антивирус или политика браузера могут блокировать 127.0.0.1:24652; иначе используйте маршрут SSH PC Bridge (docs/DEVICE_MANAGER.md).',
          ],
        ],
      ),
    );
  else if (probe.kind === 'origin')
    steps.push(
      make(
        'bridge',
        bridgeTitle,
        'failed',
        [
          'Bridge answered but does not accept this SymPort address.',
          'Bridge ответил, но не принимает этот адрес SymPort.',
        ],
        'origin-rejected',
        [
          [
            'Use the exact HTTPS address registered for SymPort (no path, no credentials) or localhost.',
            'Используйте точный HTTPS-адрес SymPort (без пути и учётных данных) или localhost.',
          ],
          ['Update Bridge if it is an older build.', 'Обновите Bridge, если сборка старая.'],
        ],
      ),
    );
  else
    steps.push(
      make('bridge', bridgeTitle, 'success', [
        probe.version
          ? `Bridge ${probe.version} answers on this PC.`
          : 'Bridge answers on this PC.',
        probe.version
          ? `Bridge ${probe.version} отвечает на этом ПК.`
          : 'Bridge отвечает на этом ПК.',
      ]),
    );

  // 3 — pairing
  const pairTitle: [string, string] = ['Access is approved', 'Доступ разрешён'];
  const bridgeOk = steps[1]?.status === 'success';
  if (relayUp)
    steps.push(
      make('pairing', pairTitle, 'success', [
        `The outbound relay is up${input.connection?.expires_in ? ` (access expires in ${Math.ceil(input.connection.expires_in / 60)} min)` : ''}.`,
        `Исходящее соединение активно${input.connection?.expires_in ? ` (доступ истекает через ${Math.ceil(input.connection.expires_in / 60)} мин)` : ''}.`,
      ]),
    );
  else if (input.pair === 'loading')
    steps.push(
      make('pairing', pairTitle, 'loading', [
        'Approve this SymPort address in the Bridge dialog on the PC (90 s).',
        'Подтвердите этот адрес SymPort в окне Bridge на ПК (90 с).',
      ]),
    );
  else if (input.pair?.kind === 'ok' && input.bridgeRelay?.state === 'error')
    steps.push(relayFailure(input.bridgeRelay.reason, pairTitle, make));
  else if (input.pair?.kind === 'ok' && input.grace)
    steps.push(
      make('pairing', pairTitle, 'loading', [
        'Approved. Waiting for Bridge to connect…',
        'Разрешено. Ждём подключения Bridge…',
      ]),
    );
  else if (input.pair && input.pair.kind !== 'ok')
    steps.push(pairFailure(input.pair.kind, pairTitle, make));
  else if (input.connection?.mode === 'windows-bridge' || input.pair?.kind === 'ok')
    steps.push(
      make(
        'pairing',
        pairTitle,
        'failed',
        ['The Bridge connection was lost.', 'Связь с Bridge потеряна.'],
        'bridge-lost',
        [
          [
            'Check that the tray icon is still there; restart Bridge if needed.',
            'Проверьте, что значок в трее на месте; при необходимости перезапустите Bridge.',
          ],
          [
            'Request access again (access lasts two hours and is dropped when Bridge restarts). Nothing is retried automatically.',
            'Запросите доступ снова (он действует два часа и сбрасывается при перезапуске Bridge). Ничего не повторяется автоматически.',
          ],
        ],
      ),
    );
  else if (direct)
    steps.push(
      make('pairing', pairTitle, 'skipped', [
        'No pairing with local transports.',
        'Сопряжение с локальными транспортами не нужно.',
      ]),
    );
  else
    steps.push(
      make(
        'pairing',
        pairTitle,
        'pending',
        bridgeOk ? ['Press “Request access”.', 'Нажмите «Запросить доступ».'] : waiting,
      ),
    );

  // 4 — USB / host candidates (candidates are never phones)
  const usbTitle: [string, string] = ['Nokia USB candidates', 'Кандидаты Nokia по USB'];
  const host = input.host;
  if (!reachable) steps.push(make('usb', usbTitle, 'pending', waiting));
  else if (host === 'loading')
    steps.push(
      make('usb', usbTitle, 'loading', [
        'Reading USB, COM and drive records on the PC…',
        'Читаем записи USB, COM и дисков на ПК…',
      ]),
    );
  else if (host === 'error')
    steps.push(
      make(
        'usb',
        usbTitle,
        'failed',
        ['The PC could not enumerate devices.', 'ПК не смог перечислить устройства.'],
        'host-failed',
        [
          [
            'Retry; if it persists, check the Bridge diagnostics from its tray menu.',
            'Повторите; если не помогает, откройте диагностику Bridge в меню трея.',
          ],
        ],
      ),
    );
  else if (host === null)
    steps.push(
      make('usb', usbTitle, 'pending', [
        'Press “Re-run diagnostics”.',
        'Нажмите «Повторить диагностику».',
      ]),
    );
  else {
    const serial = host.serial.length,
      volumes = host.volumes.length,
      bluetooth = host.bluetooth.length,
      usb = host.usb;
    const counts = `COM ${serial} · ${t('drives', 'диски')} ${volumes} · USB ${usb.length} · Bluetooth ${bluetooth}`;
    const portable = host.portable ?? [];
    const mtpStorages = portable.reduce((n, p) => n + p.storages.length, 0);
    if (!serial && !volumes && mtpStorages)
      steps.push(
        make('usb', usbTitle, 'success', [
          `${counts}. Files are readable through Windows (MTP, read-only: ${mtpStorages} storage(s)). This is not PC Suite: contacts, calendar and SMS need the Nokia driver and a COM port.`,
          `${counts}. Файлы читаются через Windows (MTP, только чтение: хранилищ ${mtpStorages}). Это не PC Suite: контактам, календарю и SMS нужны драйвер Nokia и COM-порт.`,
        ]),
      );
    else if (serial || volumes)
      steps.push(
        make('usb', usbTitle, 'success', [
          `${counts}. Candidates only — a phone is confirmed when it answers (step 6).`,
          `${counts}. Это только кандидаты: телефон подтверждается ответом (шаг 6).`,
        ]),
      );
    else if (usb.length && usb.some((u) => u.status === 'problem'))
      steps.push(
        make(
          'usb',
          usbTitle,
          'failed',
          [
            'A Nokia USB device is present, but its driver is not working.',
            'Устройство Nokia по USB видно, но драйвер не работает.',
          ],
          'driver-missing',
          [
            [
              'Install a trusted Nokia connectivity driver for your Windows version (SymPort does not install or replace drivers).',
              'Установите доверенный драйвер подключения Nokia для вашей версии Windows (SymPort драйверы не устанавливает и не подменяет).',
            ],
            [
              'In Device Manager the entry should show a COM port or a drive after switching to PC Suite.',
              'В диспетчере устройств после переключения в PC Suite должен появиться COM-порт или диск.',
            ],
          ],
        ),
      );
    else if (usb.length && usb.every((u) => u.class === 'WPD'))
      steps.push(
        make(
          'usb',
          usbTitle,
          'failed',
          [
            'Windows sees the phone only as a media device (MTP): no COM port and no drive letter.',
            'Windows видит телефон только как медиа-устройство (MTP): COM-порта и буквы диска нет.',
          ],
          'mtp-only',
          [
            [
              'For files choose Mass Storage on the phone: the memory card then gets a drive letter (SymPort does not read MTP yet).',
              'Для файлов выберите на телефоне Mass Storage: карта памяти получит букву диска (SymPort пока не читает MTP).',
            ],
            [
              'For contacts, calendar and SMS the Nokia PC Suite / Connectivity Cable driver must provide a COM port: install it yourself from Nokia/Microsoft, choose PC Suite and replug. If the phone says PC Suite but Windows shows only a portable device, that driver is most likely missing.',
              'Для контактов, календаря и SMS COM-порт должен дать драйвер Nokia PC Suite / Connectivity Cable: установите его сами у Nokia/Microsoft, выберите PC Suite и переподключите кабель. Если на телефоне выбран PC Suite, а Windows показывает только переносное устройство, скорее всего этого драйвера нет.',
            ],
          ],
        ),
      );
    else if (usb.length)
      steps.push(
        make(
          'usb',
          usbTitle,
          'failed',
          [
            'Nokia is on USB but exposes neither a COM port nor a drive.',
            'Nokia виден по USB, но не показывает ни COM-порт, ни диск.',
          ],
          'wrong-usb-mode',
          [
            [
              'On the phone choose PC Suite (for contacts, calendar, SMS) or Mass Storage (card files only) when the cable is connected.',
              'На телефоне при подключении кабеля выберите PC Suite (контакты, календарь, SMS) или Mass Storage (только файлы карты).',
            ],
            [
              'Unlock the phone and replug the cable.',
              'Разблокируйте телефон и переподключите кабель.',
            ],
          ],
        ),
      );
    else if (bluetooth)
      steps.push(
        make('usb', usbTitle, 'success', [
          `${counts}. Paired Bluetooth records only; USB is not seen.`,
          `${counts}. Найдены только записи Bluetooth; USB не виден.`,
        ]),
      );
    else
      steps.push(
        make(
          'usb',
          usbTitle,
          'failed',
          [
            direct
              ? 'No Nokia USB device is visible on the SymPort server.'
              : 'No Nokia USB device is visible on the Windows PC.',
            direct
              ? 'Nokia по USB на сервере SymPort не виден.'
              : 'Nokia по USB на Windows-ПК не виден.',
          ],
          'no-usb-device',
          [
            [
              'Use the data cable that came with the phone: a charge-only cable shows nothing.',
              'Используйте кабель для передачи данных: кабель только для зарядки ничего не покажет.',
            ],
            [
              'Switch the phone on and unlock it, try another USB port, close Nokia Suite.',
              'Включите и разблокируйте телефон, попробуйте другой порт USB, закройте Nokia Suite.',
            ],
            ...(host.diagnostics.length
              ? ([
                  [
                    `Host notes: ${host.diagnostics.join(' · ')}`,
                    `Замечания ПК: ${host.diagnostics.join(' · ')}`,
                  ],
                ] as [string, string][])
              : []),
          ],
        ),
      );
  }

  // 5 — COM / driver
  const driverTitle: [string, string] = ['Nokia COM port and driver', 'COM-порт и драйвер Nokia'];
  const devices = Array.isArray(input.devices) ? input.devices : [];
  const reasons = [
    ...input.rows.filter((r) => !r.connected).map((r) => r.failure?.reason),
    ...phoneRows(devices)
      .filter((d) => !d.connected)
      .map((d) => d.failure?.reason),
  ].filter((r): r is string => Boolean(r));
  const portReason = reasons.find((r) => PORT_REASONS.includes(r));
  if (!reachable || host === null || host === 'loading')
    steps.push(make('driver', driverTitle, 'pending', waiting));
  else if (portReason) steps.push(driverFailure(portReason, driverTitle, make));
  else if (host !== 'error' && host.serial.length)
    steps.push(
      make('driver', driverTitle, 'success', [
        `${host.serial.length} Nokia COM port(s) are visible.`,
        `Видно COM-портов Nokia: ${host.serial.length}.`,
      ]),
    );
  else if (host !== 'error' && host.volumes.length && !host.serial.length)
    steps.push(
      make('driver', driverTitle, 'unsupported', [
        'No PC Suite COM port: only the memory card (Mass Storage) is available — files only, no contacts, SMS, calendar or installed-app list.',
        'Нет COM-порта PC Suite: доступна только карта памяти (Mass Storage) — только файлы, без контактов, SMS, календаря и списка приложений.',
      ]),
    );
  else if (
    host !== 'error' &&
    (host.portable ?? []).some((p) => p.storages.length) &&
    !host.serial.length
  )
    steps.push(
      make('driver', driverTitle, 'unsupported', [
        'No PC Suite COM port: only files over Windows MTP (read-only). Contacts, calendar and SMS need the Nokia Connectivity Cable driver (installed by you) and PC Suite mode.',
        'Нет COM-порта PC Suite: доступны только файлы по MTP Windows (чтение). Контактам, календарю и SMS нужны драйвер Nokia Connectivity Cable (ставите вы) и режим PC Suite.',
      ]),
    );
  else
    steps.push(
      make('driver', driverTitle, 'pending', [
        'Needs a Nokia COM port or a configured Bluetooth profile.',
        'Нужен COM-порт Nokia или настроенный профиль Bluetooth.',
      ]),
    );

  // 6 — identity
  const idTitle: [string, string] = ['Nokia is identified', 'Nokia идентифицирован'];
  const phones = phoneRows(devices);
  const identified = phones.find((d) => d.connected && d.model);
  if (!reachable) steps.push(make('identify', idTitle, 'pending', waiting));
  else if (input.devices === 'loading')
    steps.push(make('identify', idTitle, 'loading', ['Asking the phone…', 'Опрашиваем телефон…']));
  else if (input.devices === 'error')
    steps.push(
      make(
        'identify',
        idTitle,
        'failed',
        ['The phone list could not be read.', 'Не удалось получить список подключений.'],
        'discover-failed',
        [
          [
            'Retry the diagnostics; if the Bridge is gone, request access again.',
            'Повторите диагностику; если Bridge пропал, запросите доступ снова.',
          ],
        ],
      ),
    );
  else if (input.lostIdentity)
    steps.push(
      make(
        'identify',
        idTitle,
        'failed',
        [
          'The phone was identified earlier and has disconnected.',
          'Телефон был опознан и отключился.',
        ],
        'device-lost',
        [
          [
            'Check the cable and the USB mode on the phone, then re-run diagnostics. Read the phone before repeating any write.',
            'Проверьте кабель и режим USB на телефоне, затем повторите диагностику. Прочитайте телефон, прежде чем повторять запись.',
          ],
        ],
      ),
    );
  else if (identified)
    steps.push(
      make('identify', idTitle, 'success', [
        `${identified.model}${identified.rm ? ` · ${identified.rm}` : ''}${identified.firmware ? ` · ${identified.firmware}` : ''}`,
        `${identified.model}${identified.rm ? ` · ${identified.rm}` : ''}${identified.firmware ? ` · ${identified.firmware}` : ''}`,
      ]),
    );
  else if (reasons.includes('unsupported-model'))
    steps.push(
      make(
        'identify',
        idTitle,
        'unsupported',
        [
          'The phone answered but is not a supported Nokia S60 3rd/5th Edition model.',
          'Телефон ответил, но это не поддерживаемая модель Nokia S60 3rd/5th Edition.',
        ],
        'unsupported-model',
      ),
    );
  else if (phones.length || input.rows.length)
    steps.push(
      make(
        'identify',
        idTitle,
        'failed',
        [
          'A connection is configured but the phone does not answer.',
          'Подключение настроено, но телефон не отвечает.',
        ],
        reasons.includes('no-response') ? 'no-response' : 'not-identified',
        [
          [
            'Choose PC Suite on the phone, unlock it and replug the cable; close Nokia Suite if it holds the port.',
            'Выберите PC Suite на телефоне, разблокируйте его и переподключите кабель; закройте Nokia Suite, если она занимает порт.',
          ],
          [
            'A charge-only cable or Mass Storage mode never answers over Gammu.',
            'Кабель только для зарядки или режим Mass Storage по Gammu не отвечают.',
          ],
        ],
      ),
    );
  else
    steps.push(
      make('identify', idTitle, 'pending', [
        'No phone connection is configured yet. Bridge discovers Nokia USB automatically; or add a Gammu/Bluetooth profile in its settings.',
        'Подключение к телефону ещё не настроено. Bridge находит Nokia по USB автоматически; либо добавьте профиль Gammu/Bluetooth в его настройках.',
      ]),
    );

  // 7 — transport
  const trTitle: [string, string] = ['Connection and mode', 'Подключение и режим'];
  const connected = devices.filter((d) => d.connected);
  const selected = devices.find((d) => d.id === input.selectedId);
  if (!reachable || !devices.length) steps.push(make('transport', trTitle, 'pending', waiting));
  else if (selected && selected.connected) {
    const mode = transportMode(selected);
    steps.push(
      make('transport', trTitle, 'success', [
        `${selected.name ?? selected.id}: ${t(...MODE_TEXT[mode])}`,
        `${selected.name ?? selected.id}: ${t(...MODE_TEXT[mode])}`,
      ]),
    );
  } else if (connected.length)
    steps.push(
      make('transport', trTitle, 'pending', [
        'Choose one of the connected connections below.',
        'Выберите одно из подключённых соединений ниже.',
      ]),
    );
  else
    steps.push(
      make('transport', trTitle, 'pending', [
        'No connection answers yet.',
        'Пока ни одно подключение не отвечает.',
      ]),
    );

  // 8 — capabilities
  const capTitle: [string, string] = ['Confirmed capabilities', 'Подтверждённые возможности'];
  const row = selected && selected.connected ? selected : undefined;
  if (!row) steps.push(make('capabilities', capTitle, 'pending', waiting));
  else if (!row.capability_matrix)
    steps.push(
      make(
        'capabilities',
        capTitle,
        'failed',
        [
          'This component reported no probe results, so nothing is treated as available.',
          'Компонент не прислал результаты проверок, поэтому ничего не считается доступным.',
        ],
        'no-matrix',
        [
          [
            'Update the Windows Bridge to the current build.',
            'Обновите Windows Bridge до текущей сборки.',
          ],
        ],
      ),
    );
  else {
    const available = Object.entries(row.capability_matrix)
      .filter(([, s]) => s.status === 'available')
      .map(([op]) => op);
    steps.push(
      available.length
        ? make('capabilities', capTitle, 'success', [
            `Verified by a read-only probe: ${available.join(', ')}. Writes are never probed; they are confirmed on first use.`,
            `Подтверждено безопасной проверкой чтения: ${available.join(', ')}. Запись не проверяется заранее и подтверждается при первом использовании.`,
          ])
        : make(
            'capabilities',
            capTitle,
            'failed',
            [
              'No service has been verified on this connection.',
              'На этом подключении ни одна служба не подтверждена.',
            ],
            'nothing-verified',
            [
              [
                'Recheck (read-only) or switch to a connection that answers.',
                'Перепроверьте (только чтение) или выберите подключение, которое отвечает.',
              ],
            ],
          ),
    );
  }
  return steps;
}

type Maker = (
  id: StepId,
  title: [string, string],
  status: StepStatus,
  summary: [string, string],
  code?: string | null,
  fixes?: [string, string][],
) => Step;

function pairFailure(
  kind: Exclude<PairResult['kind'], 'ok'>,
  title: [string, string],
  make: Maker,
): Step {
  const table: Record<typeof kind, { summary: [string, string]; fixes: [string, string][] }> = {
    declined: {
      summary: ['Access was declined on the PC.', 'Доступ отклонён на ПК.'],
      fixes: [
        [
          'Request access again and choose “Allow” for the exact SymPort address shown in the Windows dialog.',
          'Запросите доступ снова и выберите «Разрешить» для точного адреса SymPort в окне Windows.',
        ],
      ],
    },
    timeout: {
      summary: ['Nobody approved the request in time.', 'Запрос не подтвердили вовремя.'],
      fixes: [
        [
          'Look for the Bridge dialog (it may be behind other windows) and request access again.',
          'Найдите окно Bridge (оно может быть под другими окнами) и запросите доступ снова.',
        ],
      ],
    },
    wait: {
      summary: [
        'Too many requests; wait a few seconds.',
        'Слишком частые запросы; подождите несколько секунд.',
      ],
      fixes: [
        [
          'Wait about 15 seconds and press “Request access” once.',
          'Подождите около 15 секунд и нажмите «Запросить доступ» один раз.',
        ],
      ],
    },
    busy: {
      summary: [
        'Bridge is busy or pairing is unavailable.',
        'Bridge занят или сопряжение недоступно.',
      ],
      fixes: [
        [
          'Answer or close the open Bridge dialog, or restart Bridge, then retry.',
          'Ответьте на открытое окно Bridge или закройте его, либо перезапустите Bridge и повторите.',
        ],
      ],
    },
    unreachable: {
      summary: [
        'Bridge stopped answering during pairing.',
        'Bridge перестал отвечать во время сопряжения.',
      ],
      fixes: [
        [
          'Restart Bridge and run “Check Bridge” again.',
          'Перезапустите Bridge и снова нажмите «Проверить Bridge».',
        ],
      ],
    },
    invalid: {
      summary: ['Bridge refused the pairing request.', 'Bridge отклонил запрос сопряжения.'],
      fixes: [
        [
          'Check that SymPort is opened at an address Bridge accepts (HTTPS or localhost) and update Bridge.',
          'Проверьте, что SymPort открыт по адресу, который принимает Bridge (HTTPS или localhost), и обновите Bridge.',
        ],
      ],
    },
  };
  const { summary, fixes } = table[kind];
  return make('pairing', title, 'failed', summary, `pair-${kind}`, fixes);
}

function driverFailure(reason: string, title: [string, string], make: Maker): Step {
  const table: Record<string, { summary: [string, string]; fixes: [string, string][] }> = {
    'port-busy': {
      summary: ['The COM port is held by another program.', 'COM-порт занят другой программой.'],
      fixes: [
        [
          'Close Nokia Suite / PC Suite and any other Gammu session, then re-run diagnostics.',
          'Закройте Nokia Suite / PC Suite и другие сеансы Gammu, затем повторите диагностику.',
        ],
      ],
    },
    'port-open': {
      summary: [
        'The COM port cannot be opened (busy, unknown or no permission).',
        'COM-порт не открывается (занят, неизвестен или нет прав).',
      ],
      fixes: [
        [
          'Close programs that use the phone, replug the cable and retry.',
          'Закройте программы, использующие телефон, переподключите кабель и повторите.',
        ],
      ],
    },
    'port-missing': {
      summary: ['The configured port does not exist.', 'Указанный порт не существует.'],
      fixes: [
        [
          'The COM number changes when the cable is moved; correct it in the Bridge settings or use automatic discovery.',
          'Номер COM меняется при смене порта USB; исправьте его в настройках Bridge или используйте автообнаружение.',
        ],
      ],
    },
    'port-permission': {
      summary: ['No permission to open the port.', 'Нет прав на открытие порта.'],
      fixes: [
        [
          'Run the Bridge as the same user that owns the port and close other programs.',
          'Запустите Bridge от пользователя, которому доступен порт, и закройте другие программы.',
        ],
      ],
    },
    'driver-missing': {
      summary: [
        'There is no working driver for the Nokia device.',
        'Для устройства Nokia нет рабочего драйвера.',
      ],
      fixes: [
        [
          'Install a trusted Nokia connectivity driver separately; SymPort never installs or swaps drivers.',
          'Установите доверенный драйвер подключения Nokia отдельно; SymPort драйверы не устанавливает и не подменяет.',
        ],
      ],
    },
    'gammu-missing': {
      summary: [
        'Neither python-gammu nor the gammu program is available.',
        'Нет ни python-gammu, ни программы gammu.',
      ],
      fixes: [
        [
          'Use the current Windows Bridge (it bundles python-gammu) or install Gammu.',
          'Используйте актуальный Windows Bridge (python-gammu входит в сборку) либо установите Gammu.',
        ],
      ],
    },
    'config-invalid': {
      summary: ['The Gammu configuration is invalid.', 'Конфигурация Gammu некорректна.'],
      fixes: [
        [
          'Open the Bridge tray Settings and fix or clear the Gammu configuration.',
          'Откройте «Настройки» Bridge в трее и исправьте или очистите конфигурацию Gammu.',
        ],
      ],
    },
  };
  const fallback = {
    summary: ['The COM port cannot be used.', 'COM-порт недоступен.'] as [string, string],
    fixes: [] as [string, string][],
  };
  const entry = table[reason] ?? fallback;
  return make('driver', title, 'failed', entry.summary, reason, entry.fixes);
}

export type Levels = Record<'bridge' | 'usb' | 'identified' | 'service' | 'operation', boolean>;
/** Five different facts that must never be conflated. */
export function computeLevels(input: WizardInput): Levels {
  const devices = Array.isArray(input.devices) ? input.devices : [];
  const host = typeof input.host === 'object' && input.host ? input.host : null;
  const selected =
    devices.find((d) => d.id === input.selectedId) ?? devices.find((d) => d.connected);
  const ops = Object.values(selected?.capability_matrix ?? {});
  return {
    bridge: input.connection?.mode === 'direct' || Boolean(input.connection?.connected),
    usb: Boolean(host && (host.serial.length || host.volumes.length || host.usb.length)),
    identified: devices.some((d) => !filesOnly(d) && d.connected && d.model),
    service: ops.some((s) => s.status === 'available'),
    operation: ops.some((s) => s.source === 'observed-operation'),
  };
}
