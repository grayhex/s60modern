import { useProfiles } from '../api/queries';
import { PageHeader, StatusIndicator } from '../components/kit';
import { bytes } from '../utils/files';
import { useConfirmedAction } from '../hooks/useConfirmedAction';
import { packageLink } from '../utils/packages';
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useApi, usePreferences } from '../state/app';
import {
  catalogSchema,
  catalogPlanSchema,
  guideSchema,
  runtimeRequirementsSchema,
  type CatalogApp,
} from '../api/catalog';
import { objectSchema, text, type payload as PayloadType } from '../api/workspace';
import { Card, DeviceSelect, ErrorMessage, DevOnly } from '../components/ui';
import { Field, Modal, JsonReport } from '../components/workspace';

export default function Catalog() {
  const api = useApi(),
    { t, language, device, setDevice } = usePreferences(),
    cache = useQueryClient();
  const state = useQuery({
    queryKey: ['catalog'],
    queryFn: ({ signal }) => api.request('/api/catalog', { action: 'list' }, catalogSchema, signal),
    refetchInterval: (q) => (q.state.data?.jobs.some((j) => j.state === 'running') ? 1500 : false),
  });
  const [selected, setSelected] = useState<Set<string>>(new Set()),
    [search, setSearch] = useState(''),
    [filters, setFilters] = useState<Record<string, string>>({}),
    [detail, setDetail] = useState<CatalogApp | null>(null),
    [path, setPath] = useState(''),
    [setName, setSetName] = useState(''),
    [allProfiles, setAllProfiles] = useState(false),
    [picked, setProfile] = useState(''),
    profiles = useProfiles(),
    profile = picked || profiles[0]?.value || '';
  const plan = useMutation({
    mutationFn: () =>
      api.request(
        '/api/catalog',
        { action: 'plan', ids: [...selected], profile },
        catalogPlanSchema,
      ),
  });
  const operation = useMutation({
    mutationFn: async (body: ReturnType<typeof PayloadType>) =>
      api.request('/api/catalog', body, objectSchema),
    onSuccess: async () => {
      await cache.invalidateQueries({ queryKey: ['catalog'] });
      await cache.invalidateQueries({ queryKey: ['collections'] });
    },
  });
  const confirm = useConfirmedAction<{ action: string; job?: string; app?: string }>();
  const localized = (app: CatalogApp, key: string) =>
    text(app[language === 'en' && typeof app[key + '_en'] === 'string' ? key + '_en' : key]);
  const forModel = (a: CatalogApp) => a.model_compatibility?.[profile]?.status ?? 'not-tested';
  const visible =
    state.data?.apps.filter(
      (a) =>
        (a.category !== 'high-risk' || filters.category === 'high-risk') &&
        (allProfiles || !['unsupported', 'unavailable'].includes(forModel(a))) &&
        `${a.name} ${a.description} ${text(a.description_en)}`
          .toLocaleLowerCase()
          .includes(search.toLocaleLowerCase()) &&
        Object.entries(filters).every(([k, v]) =>
          !v || k === 'collection'
            ? !v || a.collection.includes(v)
            : (k === 'compatibility' ? forModel(a) : text(a[k])) === v,
        ),
    ) ?? [];
  function change(ids: Set<string>) {
    setSelected(ids);
    plan.reset();
  }
  const blocked =
    !plan.data ||
    !!plan.data.problems.length ||
    operation.isPending ||
    state.data?.jobs.some((j) => j.state === 'running');
  return (
    <>
      <PageHeader title={t('App Catalog', 'Каталог приложений')} />
      <Card>
        <div className="react-fields">
          <Field label={t('Target profile', 'Целевой профиль')}>
            <select
              value={allProfiles ? 'all' : profile}
              onChange={(e) => {
                setAllProfiles(e.target.value === 'all');
                if (e.target.value !== 'all') setProfile(e.target.value);
                plan.reset();
              }}
            >
              {profiles.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
              <option value="all">
                {t('All compatibility states', 'Все статусы совместимости')}
              </option>
            </select>
          </Field>
          <Field label={t('Search', 'Поиск')}>
            <input value={search} onChange={(e) => setSearch(e.target.value)} />
          </Field>
          {['category', 'compatibility', 'network', 'format', 'delivery', 'collection'].map((k) => (
            <Field
              key={k}
              label={t(
                {
                  category: 'Category',
                  compatibility: 'Compatibility',
                  network: 'Network',
                  format: 'Format',
                  delivery: 'Delivery',
                  collection: 'Collection',
                }[k] ?? k,
                {
                  category: 'Категория',
                  compatibility: 'Совместимость',
                  network: 'Сеть',
                  format: 'Формат',
                  delivery: 'Получение',
                  collection: 'Подборка',
                }[k] ?? k,
              )}
            >
              <select
                value={filters[k] ?? ''}
                onChange={(e) => setFilters({ ...filters, [k]: e.target.value })}
              >
                <option value="">{t('All', 'Все')}</option>
                {[
                  ...new Set(
                    state.data?.apps.flatMap((a) =>
                      k === 'collection'
                        ? a.collection
                        : [k === 'compatibility' ? forModel(a) : text(a[k])],
                    ),
                  ),
                ]
                  .sort()
                  .map((v) => (
                    <option key={v}>{v}</option>
                  ))}
              </select>
            </Field>
          ))}
        </div>
        <div className="form-row">
          <button
            className="btn ghost"
            onClick={() =>
              change(
                new Set([
                  ...selected,
                  ...visible
                    .filter((a) => a.delivery === 'automatic' && a.category !== 'high-risk')
                    .map((a) => a.id),
                ]),
              )
            }
          >
            {t('Select visible', 'Выбрать видимые')}
          </button>
          <button
            className="btn ghost"
            onClick={() => {
              change(new Set());
              setFilters({});
              setSearch('');
            }}
          >
            {t('Reset', 'Сбросить')}
          </button>
          <span>
            {visible.length} · {selected.size} {t('selected', 'выбрано')}
          </span>
        </div>
        <ErrorMessage error={state.error} />
      </Card>
      <div className="catalog-grid">
        {visible.map((app) => (
          <article className="catalog-app" key={app.id}>
            <div className="catalog-app-head">
              <span className="catalog-app-icon" aria-hidden="true">
                {app.name.slice(0, 2).toUpperCase()}
              </span>
              <div className="catalog-app-title">
                <h3>{app.name}</h3>
                <span className="k-meta">
                  {app.version} · {app.format} · {app.category}
                </span>
              </div>
              <input
                type="checkbox"
                aria-label={`${t('Select', 'Выбрать')} ${app.name}`}
                checked={selected.has(app.id)}
                disabled={app.delivery !== 'automatic' || app.category === 'high-risk'}
                onChange={(e) => {
                  const next = new Set(selected);
                  if (e.target.checked) next.add(app.id);
                  else next.delete(app.id);
                  change(next);
                }}
              />
            </div>
            <p className="catalog-description">{localized(app, 'description')}</p>
            <div className="catalog-app-tags">
              {/* Where the file comes from, and how much is known about compatibility: two different facts. */}
              <StatusIndicator tone={app.delivery === 'automatic' ? 'ok' : 'warn'}>
                {app.delivery === 'automatic'
                  ? t('Pinned installer (SHA-256)', 'Проверенный установщик (SHA-256)')
                  : t('External link', 'Внешняя ссылка')}
              </StatusIndicator>
              <StatusIndicator tone={forModel(app) === 'verified' ? 'info' : 'unknown'}>
                {forModel(app) === 'not-tested'
                  ? t('No test results', 'Нет результатов испытаний')
                  : forModel(app)}
              </StatusIndicator>
              <span className="k-meta">{app.network}</span>
            </div>
            {app.delivery_reason && (
              <p className="k-note-line k-clamp-2">{localized(app, 'delivery_reason')}</p>
            )}
            <div className="catalog-app-actions">
              <button className="k-btn k-btn--small k-btn--primary" onClick={() => setDetail(app)}>
                {t('Details', 'Подробнее')}
              </button>
              <DevOnly>
                <a
                  className="text-link"
                  href={packageLink(
                    'dev/lab',
                    app.delivery === 'automatic' ? 'catalog' : 'upload',
                    app.id,
                    'app',
                    profile,
                  )}
                >
                  {t('Test in emulator', 'Проверить в эмуляторе')}
                </a>
              </DevOnly>
              <DevOnly>
                <a
                  className="text-link"
                  href={packageLink(
                    'tools/certificates',
                    app.delivery === 'automatic' ? 'catalog' : 'upload',
                    app.id,
                    'app',
                    profile,
                  )}
                >
                  {t('Inspect', 'Анализ')}
                </a>
              </DevOnly>
              {app.delivery === 'manual' && app.manual_url?.startsWith('https://') && (
                <a className="text-link" href={app.manual_url} target="_blank" rel="noreferrer">
                  {t('Author source', 'Источник автора')} ↗
                </a>
              )}
              {app.delivery === 'manual' && (
                <a className="text-link" href="#apps/installed">
                  {t('Transfer your package', 'Передать свой пакет')} →
                </a>
              )}
            </div>
          </article>
        ))}
      </div>
      <Card>
        <h2>{t('Selected set', 'Выбранный набор')}</h2>
        <p>
          {state.data?.apps
            .filter((a) => selected.has(a.id))
            .map((a) => a.name)
            .join(' · ')}
        </p>
        <div className="form-row">
          <a className="btn ghost" href="#apps/kits">
            {t('Manage collections & recovery', 'Наборы и восстановление')} →
          </a>
          <Field label={t('Set name', 'Название набора')}>
            <input value={setName} onChange={(e) => setSetName(e.target.value)} />
          </Field>
          <button
            className="btn secondary"
            disabled={!setName || !selected.size}
            onClick={() =>
              operation.mutate({
                action: 'collection-save',
                name: setName,
                profile,
                ids: [...selected],
              })
            }
          >
            {t('Save locally', 'Сохранить локально')}
          </button>
        </div>
        <div className="form-row">
          <button
            className="btn secondary"
            disabled={!selected.size || plan.isPending}
            onClick={() => plan.mutate()}
          >
            {t('Check dependencies', 'Проверить зависимости')}
          </button>
          <button
            className="btn primary"
            disabled={!!blocked}
            onClick={() => operation.mutate({ action: 'download', ids: [...selected], profile })}
          >
            {t('Download set', 'Загрузить набор')}
          </button>
          <DeviceSelect value={device} onChange={setDevice} capability="transfer" />
          <Field label={t('Destination folder', 'Папка назначения')}>
            <input value={path} onChange={(e) => setPath(e.target.value)} />
          </Field>
          <button
            className="btn primary"
            disabled={!!blocked || !device}
            onClick={() =>
              confirm.ask(
                t(
                  'Transfer this set? Installation is completed on Nokia.',
                  'Передать набор? Установка завершается на Nokia.',
                ),
                { action: 'transfer' },
              )
            }
          >
            {t('Transfer', 'Передать')}
          </button>
        </div>
        {plan.data && (
          <>
            <p>
              {plan.data.ids.length} · {bytes(plan.data.bytes)}
            </p>
            {Object.entries(plan.data.requirements).map(([id, v]) => (
              <p key={id}>
                {id}: {v.join(' ')}
              </p>
            ))}
            {plan.data.problems.map((p) => (
              <p className="error-text" key={p}>
                {p}
              </p>
            ))}
          </>
        )}
        <ErrorMessage error={operation.error ?? plan.error} />
      </Card>
      <Card>
        <h2>{t('Operations', 'Операции')}</h2>
        {state.data?.jobs.map((job) => (
          <article className="catalog-job" key={job.id}>
            <h3>
              {job.action} · {job.created}
            </h3>
            <p>{job.state}</p>
            {job.error && <p className="error-text">{job.error}</p>}
            {job.items.map((item) => (
              <div className="catalog-result" key={item.id}>
                <strong>{state.data?.apps.find((a) => a.id === item.id)?.name ?? item.id}</strong>
                <span className="chip">{item.stage}</span>
                {item.error && <p className="error-text">{item.error}</p>}
                <JsonReport value={item} />
                {item.stage === 'awaiting-installation' && (
                  <button
                    className="btn secondary"
                    onClick={() =>
                      confirm.ask(
                        t(
                          'Did you install and test this application on Nokia?',
                          'Вы установили и проверили приложение на Nokia?',
                        ),
                        { action: 'confirm-installed', job: job.id, app: item.id },
                      )
                    }
                  >
                    {t('I installed it on my phone', 'Я установил на телефоне')}
                  </button>
                )}
              </div>
            ))}
            {job.state === 'completed' && (
              <a
                className="text-link"
                href={'/api/catalog/archive?id=' + encodeURIComponent(job.id)}
              >
                {t('Download verified ZIP', 'Скачать проверенный ZIP')}
              </a>
            )}
            {job.items.some((i) => ['failed', 'interrupted'].includes(i.stage)) && (
              <button
                className="btn ghost"
                onClick={() =>
                  change(
                    new Set(
                      job.items
                        .filter((i) => ['failed', 'interrupted'].includes(i.stage))
                        .map((i) => i.id),
                    ),
                  )
                }
              >
                {t('Select failed for review', 'Выбрать неудачные для проверки')}
              </button>
            )}
          </article>
        ))}
      </Card>
      {confirm.dialog(async (value) => {
        await operation.mutateAsync({
          ...value,
          ids: [...selected],
          profile,
          device,
          path,
          confirm: true,
        });
      })}
      {detail && (
        <Modal title={detail.name} onClose={() => setDetail(null)}>
          <p>{localized(detail, 'description')}</p>
          <h3>
            {t('Selected model evidence', 'Результат для выбранной модели')} · {profile}
          </h3>
          <JsonReport value={detail.model_compatibility?.[profile] ?? { status: 'not-tested' }} />
          <dl className="device-details">
            {[
              'version',
              'developer',
              'license',
              'redistribution',
              'compatibility',
              'compatibility_basis',
              'e52_evidence',
              'backend',
              'tls',
              'signature',
              'checked_at',
            ].map((k) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd>{localized(detail, k)}</dd>
              </div>
            ))}
          </dl>
          {detail.sources
            .filter((s) => s.startsWith('https://'))
            .map((s) => (
              <p key={s}>
                <a className="text-link" href={s} target="_blank" rel="noreferrer">
                  {s}
                </a>
              </p>
            ))}
          <CatalogExtras app={detail} />
          <JsonReport value={detail} />
        </Modal>
      )}
    </>
  );
}

function CatalogExtras({ app }: { app: CatalogApp }) {
  const { t } = usePreferences();
  const guide = guideSchema.safeParse(app.guide);
  const runtimes = runtimeRequirementsSchema.safeParse(app.runtime_requirements);
  const conflicts = Array.isArray(app.conflicts_with) ? app.conflicts_with.map(String) : [];
  return (
    <>
      {runtimes.success && runtimes.data.length > 0 && (
        <div className="note">
          <strong>{t('Runtime required', 'Нужна среда выполнения')}</strong>
          <ul>
            {runtimes.data.map((r) => (
              <li key={r.runtime}>
                {r.name} {r.min_version ? `≥ ${r.min_version}` : ''} — {r.declared_in}.{' '}
                {t(
                  'SymPort does not supply it and cannot see whether it is installed.',
                  'SymPort её не поставляет и не видит, установлена ли она.',
                )}
              </li>
            ))}
          </ul>
          <DevOnly>
            <a className="text-link" href={packageLink('apps/dependencies', 'catalog', app.id)}>
              {t('Check dependencies →', 'Проверить зависимости →')}
            </a>
          </DevOnly>
        </div>
      )}
      {conflicts.length > 0 && (
        <p className="note">
          {t('Shares one application identity with: ', 'Один идентификатор приложения с: ')}
          {conflicts.join(', ')}.{' '}
          {t('Install only one of them.', 'Устанавливайте только одно из них.')}
        </p>
      )}
      {guide.success && (
        <section aria-label={guide.data.title}>
          <h3>{guide.data.title}</h3>
          <ol>
            {guide.data.steps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
          {guide.data.docs?.startsWith('https://') && (
            <a className="text-link" href={guide.data.docs} target="_blank" rel="noreferrer">
              {t('Author documentation', 'Документация автора')}
            </a>
          )}
        </section>
      )}
    </>
  );
}
