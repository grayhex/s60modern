import { encode } from '../utils/files';
import { PageHeader } from '../components/kit';
import { useConfirmedAction } from '../hooks/useConfirmedAction';
import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';
import { useApi, usePreferences } from '../state/app';
import { useConnectionKey, useDevices } from '../api/queries';
import {
  contactSchema,
  fileSchema,
  objectSchema,
  payload,
  personalRow,
  planSchema,
  snapshotSchema,
  text,
  writeSchema,
  type PersonalRow,
  type ChangePlan,
} from '../api/workspace';
import { Card, CapabilityHint, DeviceSelect, ErrorMessage } from '../components/ui';
import { unverifiedReason } from '../api/capabilities';
import { Download, Field, JsonReport, OperationStatus } from '../components/workspace';
import RouteBanner from '../components/RouteBanner';
type Domain = 'contacts' | 'sms' | 'calendar' | 'tasks';
const names = {
  contacts: ['Contacts', 'Контакты'],
  sms: ['Messages', 'Сообщения'],
  calendar: ['Calendar', 'Календарь'],
  tasks: ['Tasks', 'Задачи'],
} as const;
export default function Personal({ domain: initial }: { domain: Domain }) {
  const { device, setDevice, t } = usePreferences();
  const connection = useConnectionKey(device);
  const [domain, setDomain] = useState(initial);
  return (
    <>
      <PageHeader title={t(names[domain][0], names[domain][1])} />
      <RouteBanner domain={domain} />
      <Card>
        <div className="form-row">
          <DeviceSelect value={device} onChange={setDevice} />
          {initial === 'calendar' && (
            <Field label={t('Store', 'Хранилище')}>
              <select
                value={domain}
                onChange={(e) => setDomain(e.target.value === 'tasks' ? 'tasks' : 'calendar')}
              >
                <option value="calendar">{t('Calendar', 'Календарь')}</option>
                <option value="tasks">{t('Tasks', 'Задачи')}</option>
              </select>
            </Field>
          )}
        </div>
      </Card>
      <Workspace key={`${device}:${domain}:${connection}`} device={device} domain={domain} />
    </>
  );
}
function Workspace({ device, domain }: { device: string; domain: Domain }) {
  const api = useApi(),
    cache = useQueryClient(),
    { t, contactIndex, setContactIndex } = usePreferences(),
    devices = useDevices();
  const phone = devices.data?.devices.find((d) => d.id === device),
    personal = phone?.capabilities.includes('personal-data') ?? false;
  const [rows, setRows] = useState<PersonalRow[]>([]),
    [selected, setSelected] = useState<Set<number>>(new Set()),
    [search, setSearch] = useState(''),
    [page, setPage] = useState(1),
    [duplicates, setDuplicates] = useState(false),
    [plan, setPlan] = useState<ChangePlan | null>(null),
    [snapshot, setSnapshot] = useState<string | null>(null),
    [output, setOutput] = useState<z.infer<typeof fileSchema> | null>(null),
    [message, setMessage] = useState(''),
    [file, setFile] = useState<File | null>(null),
    [direction, setDirection] = useState(''),
    [thread, setThread] = useState(''),
    [number, setNumber] = useState(''),
    [sms, setSms] = useState(''),
    [editing, setEditing] = useState<PersonalRow | null>(null);
  const key = ['personal', device, domain];
  const read = useQuery({
    queryKey: key,
    queryFn: ({ signal }) =>
      api.request(
        '/api/manager',
        { action: 'personal-read', device, domain },
        snapshotSchema,
        signal,
      ),
    enabled: false,
    retry: false,
    gcTime: 0,
    refetchOnWindowFocus: false,
  });
  // Private rows and optimistic plans are never persisted. Remove the server snapshot cache on exit.
  useEffect(
    () => () => {
      cache.removeQueries({ queryKey: ['personal', device, domain] });
    },
    [cache, device, domain],
  );
  const operation = useMutation({ mutationFn: async (fn: () => Promise<void>) => fn() });
  const confirm = useConfirmedAction<'apply' | 'send' | 'stage'>();
  const call = (action: string, extra: Record<string, z.infer<typeof objectSchema>[string]> = {}) =>
    api.request('/api/manager', { action, device, ...extra }, objectSchema);
  function replace(next: PersonalRow[]) {
    setRows(next);
    setSelected(new Set(next.map((_, i) => i)));
    setPlan(null);
    setPage(1);
    setDuplicates(false);
  }
  async function load() {
    setPlan(null);
    setSnapshot(null);
    setEditing(null);
    setOutput(null);
    replace([]);
    if (personal) {
      const result = await read.refetch();
      if (result.error) throw result.error;
      if (result.data) {
        replace(result.data.rows);
        setSnapshot(result.data.snapshot);
        if (domain === 'contacts')
          setContactIndex({
            device,
            fingerprint: result.data.fingerprint,
            names: Object.fromEntries(
              result.data.rows.flatMap((row) =>
                (row.phones ?? []).map((number) => [number.replace(/\D/g, ''), row.name ?? number]),
              ),
            ),
          });
        setMessage(t('Read from phone', 'Прочитано с телефона'));
      }
    } else if (domain === 'contacts') {
      const r = await api.request(
        '/api/manager',
        { action: 'contacts-read', device },
        z.object({ contacts: z.array(contactSchema) }),
      );
      replace(r.contacts);
    } else if (domain === 'calendar' || domain === 'tasks') {
      const r = await api.request(
        '/api/manager',
        { action: 'calendar-read', device },
        z.object({
          events: z.array(personalRow),
          todos: z.array(personalRow),
          raw_ics: z.string().nullish(),
          status: z.string(),
        }),
      );
      replace(domain === 'calendar' ? r.events : r.todos);
      if (r.raw_ics) setOutput({ name: 'calendar.ics', content: r.raw_ics });
      setMessage(
        r.status === 'read'
          ? t('Read from phone', 'Прочитано с телефона')
          : t(
              'No entries returned. This driver cannot distinguish an empty store from an unsupported store.',
              'Записей нет. Драйвер не позволяет отличить пустой календарь от неподдерживаемого.',
            ),
      );
    }
  }
  async function preview(next: PersonalRow[]) {
    let id = snapshot;
    if (!id) {
      const result = await read.refetch();
      if (result.error) throw result.error;
      id = result.data?.snapshot ?? null;
      setSnapshot(id);
    }
    if (!id) throw Error(t('Read the phone first', 'Сначала прочитайте телефон'));
    setPlan(null);
    setPlan(
      await api.request(
        '/api/manager',
        payload({ action: 'personal-preview', device, snapshot: id, rows: next }),
        planSchema,
      ),
    );
  }
  async function backupDownload(id: string) {
    setOutput(
      await api.request(
        '/api/manager',
        { action: 'personal-backup-download', device, backup: id },
        fileSchema,
      ),
    );
  }
  async function backup() {
    if (!snapshot) return;
    const r = await api.request(
      '/api/manager',
      { action: 'personal-backup', device, snapshot },
      z.object({ backup: z.string() }),
    );
    await backupDownload(r.backup);
  }
  async function exportPhone(format: string) {
    if (!snapshot) return;
    setOutput(
      await api.request(
        '/api/manager',
        { action: 'personal-export', device, snapshot, format },
        fileSchema,
      ),
    );
  }
  async function apply(kind: 'apply' | 'send' | 'stage') {
    if (kind === 'stage') {
      const r = await call(
        phone?.capabilities.includes('contacts-import') ? 'contacts-import' : 'contacts-stage',
        payload({ path: '', contacts: rows.filter((_, i) => selected.has(i)), confirm: true }),
      );
      setMessage(
        `${text(r.status)} · ${text(r.folder)} · ${text(r.count)} ${r.verified != null ? `${t('Verified by read-back', 'Подтверждено чтением')}: ${text(r.verified)} / ${text(r.requested)}` : ''}`,
      );
      return;
    }
    if (!snapshot) throw Error('Snapshot expired');
    const id = snapshot;
    setSnapshot(null);
    const current = plan;
    setPlan(null);
    const result =
      kind === 'send'
        ? await api.request(
            '/api/manager',
            {
              action: 'sms-send',
              device,
              snapshot: id,
              number: number.trim(),
              text: sms,
              confirm: true,
            },
            writeSchema,
          )
        : await api.request(
            '/api/manager',
            {
              action: 'personal-apply',
              device,
              snapshot: id,
              plan: current?.plan ?? '',
              confirm: true,
            },
            writeSchema,
          );
    setMessage(
      result.status === 'verified-by-readback'
        ? `${t('Verified by read-back', 'Подтверждено повторным чтением')} · ${result.verified}`
        : result.status === 'submitted-to-network'
          ? t(
              'Submitted to network. Delivery is unknown. Read the phone before another send.',
              'Передано в сеть. Доставка неизвестна. Прочитайте телефон перед следующей отправкой.',
            )
          : t(
              'Result needs verification. Read the phone before retrying.',
              'Результат требует проверки. Прочитайте телефон перед повтором.',
            ),
    );
    if (result.backup) await backupDownload(result.backup);
  }
  const contactName = (number: string | undefined) =>
    number && contactIndex?.device === device && contactIndex.fingerprint === read.data?.fingerprint
      ? (contactIndex.names[number.replace(/\D/g, '')] ?? number)
      : number;
  const duplicateIndexes = new Set(read.data?.duplicates.flat() ?? []);
  const filtered = rows
    .map((row, index) => ({ row, index }))
    .filter(
      ({ row, index }) =>
        (!duplicates || duplicateIndexes.has(index)) &&
        (!direction || row.direction === direction) &&
        (!thread || row.number === thread) &&
        [...Object.values(row), contactName(row.number)]
          .flat()
          .join(' ')
          .toLocaleLowerCase()
          .includes(search.toLocaleLowerCase()),
    );
  // The store of this very domain must have answered; another store being readable is not enough.
  const readOp = `${domain}.read`;
  const readable =
    !!device &&
    !!phone &&
    (phone.capability_matrix
      ? unverifiedReason(phone, readOp) === null
      : personal ||
        phone.capabilities.includes(
          domain === 'sms'
            ? 'sms-read'
            : domain === 'contacts'
              ? 'contacts-read'
              : `${domain}-read`,
        ));
  function edit(index: number, patch: Partial<PersonalRow>) {
    setRows((current) => current.map((row, i) => (i === index ? { ...row, ...patch } : row)));
    setPlan(null);
  }
  const busy = operation.isPending || read.isFetching || confirm.busy;
  return (
    <>
      <Card>
        <CapabilityHint device={phone} ops={[readOp]} verified />
        <div className="form-row">
          <button
            className="btn primary"
            disabled={!readable || busy}
            onClick={() => operation.mutate(load)}
          >
            {t('Read phone', 'Прочитать телефон')}
          </button>
          <button
            className="btn ghost"
            onClick={() => {
              replace([]);
              setSnapshot(null);
              if (domain === 'contacts') setContactIndex(null);
              setOutput(null);
              setMessage('');
            }}
          >
            {t('Clear private view', 'Очистить личные данные')}
          </button>
          {personal && snapshot && (
            <>
              <button
                className="btn secondary"
                disabled={busy}
                onClick={() => operation.mutate(backup)}
              >
                {t('Back up', 'Резервная копия')}
              </button>
              {(domain === 'contacts'
                ? ['vcf']
                : domain === 'sms'
                  ? ['json', 'csv']
                  : ['json', 'ics']
              ).map((format) => (
                <button
                  key={format}
                  className="btn ghost"
                  disabled={busy}
                  onClick={() => operation.mutate(() => exportPhone(format))}
                >
                  {t('Export', 'Экспорт')} {format.toUpperCase()}
                </button>
              ))}
            </>
          )}
        </div>
        {!readable && (
          <p className="note">
            {t(
              'This connection does not expose this phone store. Mass Storage gives files only. Export with Nokia Suite, Google Contacts or Apple Contacts and import the file here.',
              'Подключение не открывает это хранилище телефона. Mass Storage предоставляет только файлы. Выполните экспорт в Nokia Suite, Google Contacts или Apple Contacts и загрузите файл.',
            )}
          </p>
        )}
        <OperationStatus pending={busy} error={operation.error ?? read.error} success={message} />
        {output && <Download {...output} auto />}
      </Card>
      {domain === 'contacts' && (
        <Card>
          <h2>{t('Import Google / Apple Contacts', 'Импорт Google / Apple Contacts')}</h2>
          <p>
            {t(
              'Export Google CSV or vCard (.vcf). Apple Contacts exports vCard. Review and edit before synchronizing or copying cards to Nokia.',
              'Экспортируйте Google CSV или vCard (.vcf). Apple Contacts экспортирует vCard. Проверьте и отредактируйте записи перед синхронизацией или передачей карточек на Nokia.',
            )}
          </p>
          <div className="form-row">
            <input
              aria-label={t('Contact file', 'Файл контактов')}
              type="file"
              accept=".csv,.vcf,.vcard"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
            <button
              className="btn secondary"
              disabled={!file || busy}
              onClick={() =>
                operation.mutate(async () => {
                  if (!file || file.size > 4 * 1024 ** 2)
                    throw Error(t('Choose CSV/vCard up to 4 MiB', 'Выберите CSV/vCard до 4 МиБ'));
                  const r = await api.request(
                    '/api/contacts',
                    { action: 'preview', name: file.name, content: await encode(file) },
                    z.object({ contacts: z.array(contactSchema), warnings: z.array(z.string()) }),
                  );
                  replace(r.contacts);
                  setMessage(r.warnings.join(' '));
                })
              }
            >
              {t('Preview file', 'Просмотреть файл')}
            </button>
          </div>
        </Card>
      )}
      {rows.length > 0 && (
        <Card>
          <div className="form-row">
            <Field label={t('Search', 'Поиск')}>
              <input
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  setPage(1);
                }}
              />
            </Field>
            {domain === 'contacts' && (
              <>
                <button
                  className="btn ghost"
                  onClick={() =>
                    setSelected(
                      selected.size === rows.length ? new Set() : new Set(rows.map((_, i) => i)),
                    )
                  }
                >
                  {t('Select all / none', 'Выбрать всё / снять')}
                </button>
                {!!duplicateIndexes.size && (
                  <button
                    className="btn ghost"
                    aria-pressed={duplicates}
                    onClick={() => {
                      setDuplicates(!duplicates);
                      setPage(1);
                    }}
                  >
                    {t('Duplicates', 'Дубли')}
                  </button>
                )}
                {['vcard', 'zip'].map((format) => (
                  <button
                    key={format}
                    className="btn secondary"
                    disabled={!selected.size || busy}
                    onClick={() =>
                      operation.mutate(async () => {
                        const r = await api.request(
                          '/api/contacts',
                          payload({
                            action: 'export',
                            contacts: rows.filter((_, i) => selected.has(i)),
                          }),
                          z.object({ vcard: z.string(), zip: z.string() }),
                        );
                        setOutput({
                          name: format === 'zip' ? 'contacts.zip' : 'contacts.vcf',
                          content: format === 'zip' ? r.zip : r.vcard,
                        });
                      })
                    }
                  >
                    {t('Export', 'Экспорт')} {format === 'zip' ? 'ZIP' : 'vCard'}
                  </button>
                ))}
              </>
            )}
            {domain === 'sms' && (
              <>
                <select
                  aria-label={t('Direction', 'Направление')}
                  value={direction}
                  onChange={(e) => setDirection(e.target.value)}
                >
                  <option value="">{t('All messages', 'Все сообщения')}</option>
                  <option value="incoming">{t('Incoming', 'Входящие')}</option>
                  <option value="outgoing">{t('Outgoing', 'Исходящие')}</option>
                </select>
                <select
                  aria-label={t('Conversation', 'Переписка')}
                  value={thread}
                  onChange={(e) => setThread(e.target.value)}
                >
                  <option value="">{t('All conversations', 'Все переписки')}</option>
                  {[...new Set(rows.map((r) => r.number))].map((n) => (
                    <option key={n} value={n}>
                      {contactName(n)}
                    </option>
                  ))}
                </select>
              </>
            )}
          </div>
          <div className="table-scroll">
            {domain === 'sms' ? (
              filtered.slice(0, page * 100).map(({ row, index }) => (
                <article className="pim-thread" key={index}>
                  <header>
                    <strong>
                      {contactName(row.number)}{' '}
                      {contactName(row.number) !== row.number ? ` · ${row.number}` : ''}
                    </strong>
                    <span>{row.date}</span>
                    <span>
                      {row.direction} · {row.state}
                      {Number(row.parts) > 1 ? ` · ${text(row.part)}/${text(row.parts)}` : ''}
                    </span>
                  </header>
                  <p>{row.text}</p>
                </article>
              ))
            ) : (
              <table>
                <thead>
                  <tr>
                    {(domain === 'contacts'
                      ? [t('Select', 'Выбор'), t('Name', 'Имя'), t('Phones', 'Телефоны'), 'Email']
                      : [
                          t('Date', 'Дата'),
                          t('Title', 'Название'),
                          t('Location', 'Место'),
                          t('Edit', 'Изменить'),
                        ]
                    ).map((h) => (
                      <th key={h}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {filtered.slice(0, page * 50).map(({ row, index }) => (
                    <tr key={index}>
                      {domain === 'contacts' ? (
                        <>
                          <td>
                            <input
                              type="checkbox"
                              aria-label={`${t('Select', 'Выбрать')} ${row.name}`}
                              checked={selected.has(index)}
                              onChange={(e) => {
                                setPlan(null);
                                setSelected((current) => {
                                  const next = new Set(current);
                                  if (e.target.checked) next.add(index);
                                  else next.delete(index);
                                  return next;
                                });
                              }}
                            />
                          </td>
                          {(['name', 'phones', 'emails'] as const).map((k) => (
                            <td key={k}>
                              <input
                                aria-label={`${k}: ${row.name}`}
                                value={k === 'name' ? (row.name ?? '') : (row[k] ?? []).join('; ')}
                                onChange={(e) =>
                                  edit(
                                    index,
                                    k === 'name'
                                      ? { name: e.target.value, given: e.target.value, family: '' }
                                      : {
                                          [k]: e.target.value
                                            .split(';')
                                            .map((x) => x.trim())
                                            .filter(Boolean),
                                        },
                                  )
                                }
                              />
                            </td>
                          ))}
                        </>
                      ) : (
                        <>
                          <td>{text(row.start) || text(row.due)}</td>
                          <td>{row.summary}</td>
                          <td>{text(row.location)}</td>
                          <td>
                            <button
                              className="btn ghost"
                              disabled={!personal || !!row.recurrence}
                              title={
                                row.recurrence
                                  ? t(
                                      'Edit recurring entries on the phone',
                                      'Повторяющиеся события редактируются на телефоне',
                                    )
                                  : undefined
                              }
                              onClick={() => {
                                setEditing({ ...row });
                                setPlan(null);
                              }}
                            >
                              {t('Edit', 'Изменить')}
                            </button>
                          </td>
                        </>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          <p>
            {Math.min(filtered.length, page * (domain === 'sms' ? 100 : 50))} / {filtered.length} ·{' '}
            {domain === 'contacts' ? selected.size : ''}
          </p>
          <button
            className="btn ghost"
            disabled={page * (domain === 'sms' ? 100 : 50) >= filtered.length}
            onClick={() => setPage(page + 1)}
          >
            {t('Show more', 'Показать ещё')}
          </button>
          {domain === 'contacts' && (
            <div className="form-row">
              {personal ? (
                <button
                  className="btn primary"
                  disabled={!selected.size || busy}
                  onClick={() =>
                    operation.mutate(() => preview(rows.filter((_, i) => selected.has(i))))
                  }
                >
                  {t('Preview synchronization', 'План синхронизации')}
                </button>
              ) : (
                <button
                  className="btn primary"
                  disabled={
                    !selected.size ||
                    busy ||
                    !phone?.capabilities.some((c) =>
                      ['contacts-stage', 'contacts-import'].includes(c),
                    )
                  }
                  onClick={() =>
                    confirm.ask(
                      phone?.capabilities.includes('contacts-import')
                        ? t(
                            'Add selected contacts into the phonebook? Save a backup first. Existing entries are not deleted.',
                            'Добавить выбранные контакты в телефонную книгу? Сначала сохраните копию. Существующие записи не удаляются.',
                          )
                        : t(
                            'Copy selected vCards to the phone? Open them on Nokia to import.',
                            'Передать выбранные vCard? Откройте их на Nokia для импорта.',
                          ),
                      'stage',
                    )
                  }
                >
                  {phone?.capabilities.includes('contacts-import')
                    ? t('Import into phonebook', 'Импортировать в книгу')
                    : t('Copy vCards to Nokia', 'Передать vCard на Nokia')}
                </button>
              )}
            </div>
          )}
        </Card>
      )}
      {(domain === 'calendar' || domain === 'tasks') && personal && (
        <Card>
          <div className="form-row">
            <button
              className="btn secondary"
              disabled={!snapshot || busy}
              onClick={() => {
                setEditing({
                  kind: domain,
                  summary: '',
                  location: '',
                  description: '',
                  start: '',
                  end: '',
                  due: '',
                  completed: false,
                });
                setPlan(null);
              }}
            >
              {t('New entry', 'Новая запись')}
            </button>
            <input
              aria-label="iCalendar"
              type="file"
              accept=".ics"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
            <button
              className="btn secondary"
              disabled={!file || !snapshot || busy}
              onClick={() =>
                operation.mutate(async () => {
                  if (!file || file.size > 4 * 1024 ** 2) throw Error('iCalendar limit: 4 MiB');
                  const r = await api.request(
                    '/api/manager',
                    { action: 'personal-calendar-import', device, text: await file.text() },
                    z.object({ rows: z.array(personalRow) }),
                  );
                  await preview(r.rows.filter((r) => r.kind === domain));
                })
              }
            >
              {t('Preview iCalendar import', 'План импорта iCalendar')}
            </button>
          </div>
          {editing && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                operation.mutate(() => preview([editing]));
              }}
            >
              <div className="react-fields">
                {[
                  'summary',
                  'location',
                  'description',
                  'start',
                  domain === 'tasks' ? 'due' : 'end',
                ].map((k) => (
                  <Field
                    key={k}
                    label={t(
                      {
                        summary: 'Title',
                        location: 'Location',
                        description: 'Description',
                        start: 'Start',
                        end: 'End',
                        due: 'Due',
                      }[k] ?? k,
                      {
                        summary: 'Название',
                        location: 'Место',
                        description: 'Описание',
                        start: 'Начало',
                        end: 'Конец',
                        due: 'Срок',
                      }[k] ?? k,
                    )}
                  >
                    <input
                      value={text(editing[k])}
                      onChange={(e) => {
                        setEditing({ ...editing, [k]: e.target.value });
                        setPlan(null);
                      }}
                    />
                  </Field>
                ))}
                {domain === 'tasks' && (
                  <Field label={t('Completed', 'Выполнено')}>
                    <select
                      value={editing.completed == null ? 'unknown' : String(editing.completed)}
                      onChange={(e) =>
                        setEditing({
                          ...editing,
                          completed:
                            e.target.value === 'unknown' ? null : e.target.value === 'true',
                        })
                      }
                    >
                      <option value="unknown">{t('Unspecified', 'Не задано')}</option>
                      <option value="false">{t('No', 'Нет')}</option>
                      <option value="true">{t('Yes', 'Да')}</option>
                    </select>
                  </Field>
                )}
              </div>
              <button className="btn primary" disabled={busy}>
                {t('Preview changes', 'Просмотреть изменения')}
              </button>
            </form>
          )}
        </Card>
      )}
      {domain === 'sms' && (
        <Card>
          <h2>{t('Send SMS', 'Отправить SMS')}</h2>
          <p className="note">
            {t(
              'Sending requires a verified phone transport. Your operator may charge for each SMS.',
              'Для отправки требуется проверенный транспорт. Оператор может взимать плату за каждое SMS.',
            )}
          </p>
          <div className="react-fields">
            <Field label={t('Recipient', 'Получатель')}>
              <input value={number} onChange={(e) => setNumber(e.target.value)} />
            </Field>
            <Field label={t('Message (70 UTF-16 units)', 'Сообщение (70 единиц UTF-16)')}>
              <textarea maxLength={70} value={sms} onChange={(e) => setSms(e.target.value)} />
            </Field>
          </div>
          <button
            className="btn primary"
            disabled={
              !snapshot ||
              !phone?.sms_send_verified ||
              !/^\+?[0-9]{3,20}$/.test(number.trim()) ||
              !sms ||
              busy
            }
            onClick={() =>
              confirm.ask(
                `${t('Send this SMS? Charges may apply.', 'Отправить SMS? Возможна плата оператора.')}\n${number}\n${sms}`,
                'send',
              )
            }
          >
            {t('Send SMS', 'Отправить SMS')}
          </button>
        </Card>
      )}
      {plan && (
        <Card>
          <h2>{t('Synchronization preview', 'План синхронизации')}</h2>
          {plan.changes.map((c, i) => (
            <div key={i}>
              <strong>{c.operation}</strong>
              <JsonReport
                value={{ before: c.before, after: c.after }}
                label={t('Before → after', 'До → после')}
              />
            </div>
          ))}
          {plan.conflicts.map((c, i) => (
            <p className="error-text" key={i}>
              {text(c.summary)} · {text(c.reason)}
            </p>
          ))}
          <p>
            {t('Skipped', 'Пропущено')}: {plan.skipped} · {t('Deletions', 'Удаления')}: 0
          </p>
          <button
            className="btn primary"
            disabled={!snapshot || !plan.changes.length || !!plan.conflicts.length || busy}
            onClick={() =>
              confirm.ask(
                t(
                  'Apply the displayed changes? A private backup is saved first. No entries will be deleted.',
                  'Применить показанные изменения? Сначала сохраняется личная резервная копия. Записи не удаляются.',
                ),
                'apply',
              )
            }
          >
            {t('Apply changes', 'Применить изменения')}
          </button>
        </Card>
      )}
      {confirm.dialog(apply)}
      <ErrorMessage error={confirm.error} />
    </>
  );
}
