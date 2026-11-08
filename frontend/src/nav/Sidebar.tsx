import { useEffect, useId, useRef, useState, type CSSProperties } from 'react';
import {
  ChevronDown,
  Code2,
  Languages,
  Layers,
  LogOut,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Settings,
  Smartphone,
  Sun,
  SunMoon,
  User,
  X,
} from 'lucide-react';
import { DARK_THEME_ENABLED, useApi, usePreferences } from '../state/app';
import { useDevices } from '../api/queries';
import { authMode } from '../api/client';
import { findGroup, visibleGroups, type NavItem } from './routes';

const base = import.meta.env.BASE_URL;

export type SidebarProps = {
  route: string;
  compact: boolean;
  onCompact: (value: boolean) => void;
  mobile: boolean;
  open: boolean;
  onClose: () => void;
  openGroups: string[];
  onGroups: (value: string[] | ((previous: string[]) => string[])) => void;
};

type Tip = { label: string; top: number } | null;

export default function Sidebar(props: SidebarProps) {
  const { route, compact, mobile, open, onClose, openGroups, onGroups } = props;
  const { t, mode } = usePreferences();
  const menu = visibleGroups(mode);
  const [tip, setTip] = useState<Tip>(null);
  const root = useRef<HTMLElement>(null);
  const railed = compact && !mobile;
  const activeGroup = findGroup(route)?.id;

  // The group that holds the current page is always expanded so the user keeps their bearings.
  useEffect(() => {
    if (activeGroup)
      onGroups((previous) =>
        previous.includes(activeGroup) ? previous : [...previous, activeGroup],
      );
  }, [activeGroup, onGroups]);

  useEffect(() => {
    if (!mobile || !open) return;
    const node = root.current;
    node?.querySelector<HTMLElement>('a, button')?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    addEventListener('keydown', key);
    return () => removeEventListener('keydown', key);
  }, [mobile, open, onClose]);

  function showTip(event: { currentTarget: HTMLElement }, label: string) {
    if (!railed) return;
    const box = event.currentTarget.getBoundingClientRect();
    setTip({ label, top: box.top + box.height / 2 });
  }
  const tipHandlers = (label: string) => ({
    onMouseEnter: (event: React.MouseEvent<HTMLElement>) => showTip(event, label),
    onFocus: (event: React.FocusEvent<HTMLElement>) => showTip(event, label),
    onMouseLeave: () => setTip(null),
    onBlur: () => setTip(null),
  });

  const hidden = mobile && !open;
  return (
    <>
      {mobile && open && <div className="nav-backdrop" onClick={onClose} aria-hidden="true" />}
      <aside
        ref={root}
        id="sidebar"
        className={`sidebar ${railed ? 'sidebar--compact' : ''} ${mobile ? 'sidebar--drawer' : ''}`}
        data-open={mobile ? open : undefined}
        aria-label={t('Main navigation', 'Основная навигация')}
        {...(hidden ? { inert: true } : {})}
      >
        <div className="sidebar-panel">
          <div className="sidebar-top">
            <a
              className="brand"
              href="#phone/overview"
              aria-label={t(
                'SymPort — Nokia S60 device workspace',
                'SymPort — рабочее место для Nokia S60',
              )}
            >
              {railed ? (
                <img className="brand-mark-img" src={`${base}brand/appmark.svg`} alt="" />
              ) : (
                <span className="brand-lockup">
                  <img
                    className="brand-wordmark brand-wordmark--light"
                    src={`${base}brand/nokia-blue.svg`}
                    alt=""
                  />
                  <img
                    className="brand-wordmark brand-wordmark--dark"
                    src={`${base}brand/nokia-white.svg`}
                    alt=""
                  />
                  <span className="brand-text">
                    <strong>SymPort</strong>
                    <small>S60 DEVICE WORKSPACE</small>
                  </span>
                </span>
              )}
            </a>
            {mobile && (
              <button
                className="icon-button"
                onClick={onClose}
                aria-label={t('Close menu', 'Закрыть меню')}
              >
                <X size={20} aria-hidden="true" />
              </button>
            )}
          </div>
          <PhoneCard railed={railed} tip={tipHandlers} />
          <ModeSwitch railed={railed} tip={tipHandlers} />
          <nav className="sidebar-menu" aria-label={t('Sections', 'Разделы')}>
            {menu.map((group) => {
              const expanded = railed || openGroups.includes(group.id);
              const regionId = `nav-region-${group.id}`;
              const GroupIcon = group.icon;
              return (
                <div className="nav-group" key={group.id} data-open={expanded}>
                  {railed ? (
                    <div
                      className="nav-divider"
                      role="separator"
                      aria-label={t(group.en, group.ru)}
                    />
                  ) : (
                    <button
                      type="button"
                      className="nav-group-head"
                      aria-expanded={expanded}
                      aria-controls={regionId}
                      onClick={() =>
                        onGroups((previous) =>
                          previous.includes(group.id)
                            ? previous.filter((id) => id !== group.id)
                            : [...previous, group.id],
                        )
                      }
                    >
                      <GroupIcon size={16} aria-hidden="true" />
                      <span>{t(group.en, group.ru)}</span>
                      <ChevronDown size={16} className="nav-chevron" aria-hidden="true" />
                    </button>
                  )}
                  <div className="nav-collapse" id={regionId} data-open={expanded}>
                    <ul {...(expanded ? {} : { inert: true })}>
                      {group.items.map((item) => (
                        <li key={item.id}>
                          <NavLinkItem
                            item={item}
                            route={route}
                            railed={railed}
                            onNavigate={onClose}
                            mobile={mobile}
                            tip={tipHandlers}
                          />
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              );
            })}
          </nav>
          <SidebarFooter
            railed={railed}
            compact={compact}
            onCompact={props.onCompact}
            tip={tipHandlers}
          />
        </div>
        {tip && (
          <div role="tooltip" className="nav-tip" style={{ top: tip.top } satisfies CSSProperties}>
            {tip.label}
          </div>
        )}
      </aside>
    </>
  );
}

type TipFactory = (label: string) => {
  onMouseEnter: (event: React.MouseEvent<HTMLElement>) => void;
  onFocus: (event: React.FocusEvent<HTMLElement>) => void;
  onMouseLeave: () => void;
  onBlur: () => void;
};

function NavLinkItem({
  item,
  route,
  railed,
  mobile,
  onNavigate,
  tip,
}: {
  item: NavItem;
  route: string;
  railed: boolean;
  mobile: boolean;
  onNavigate: () => void;
  tip: TipFactory;
}) {
  const { t } = usePreferences();
  const Icon = item.icon;
  const label = t(item.en, item.ru);
  const active = route === item.id;
  return (
    <a
      className={`nav-link ${active ? 'active' : ''}`}
      href={`#${item.id}`}
      aria-current={active ? 'page' : undefined}
      aria-label={railed ? label : undefined}
      title={railed ? undefined : label}
      onClick={() => {
        if (mobile) onNavigate();
      }}
      {...tip(label)}
    >
      <Icon size={18} strokeWidth={1.9} className="nav-icon" aria-hidden="true" />
      <span className="nav-text">{label}</span>
      {item.status === 'planned' && !railed && (
        <span className="nav-badge" title={t('In development', 'В разработке')}>
          {t('Soon', 'Скоро')}
        </span>
      )}
      {item.experimental && !railed && <span className="nav-badge nav-badge--warn">β</span>}
    </a>
  );
}

function PhoneCard({ railed, tip }: { railed: boolean; tip: TipFactory }) {
  const { t, device } = usePreferences();
  const devices = useDevices();
  const list = devices.data?.devices ?? [];
  const selected =
    list.find((d) => d.id === device) ??
    list.find((d) => d.connected && d.model) ??
    list.find((d) => d.connected);
  const state: 'checking' | 'unknown' | 'none' | 'connected' | 'disconnected' = devices.isPending
    ? 'checking'
    : devices.isError
      ? 'unknown'
      : !selected
        ? 'none'
        : selected.connected
          ? 'connected'
          : 'disconnected';
  const labels = {
    checking: t('Checking…', 'Проверка…'),
    unknown: t('Status unknown', 'Статус неизвестен'),
    none: t('No phone connected', 'Телефон не подключён'),
    connected: t('Connected', 'Подключено'),
    disconnected: t('Disconnected', 'Отключено'),
  } as const;
  const model =
    (selected?.model_profile
      ? t(
          selected.model_profile.name,
          selected.model_profile.name_ru ?? selected.model_profile.name,
        )
      : undefined) ??
    selected?.model ??
    selected?.name ??
    null;
  const title =
    model ??
    (state === 'connected'
      ? t('Model not reported', 'Модель не определена')
      : t('Nokia phone', 'Телефон Nokia'));
  const summary = `${title} — ${labels[state]}`;
  return (
    <a
      className={`phone-card phone-card--${state}`}
      href="#phone/connection"
      aria-label={t('Connection: ', 'Подключение: ') + summary}
      {...tip(summary)}
    >
      <span className="phone-card-icon" aria-hidden="true">
        {selected?.model_profile?.image ? (
          <img
            src={selected.model_profile.image}
            alt=""
            style={{ height: railed ? 28 : 38, width: 24, objectFit: 'contain' }}
          />
        ) : (
          <Smartphone size={railed ? 20 : 22} strokeWidth={1.8} />
        )}
        <span className="phone-card-dot" />
      </span>
      {!railed && (
        <span className="phone-card-text">
          <strong>{title}</strong>
          <span className="phone-card-status" role="status">
            {labels[state]}
            {selected && state === 'connected' ? ` · ${selected.transport}` : ''}
          </span>
        </span>
      )}
    </a>
  );
}

/** User mode (default) shows the everyday functions; developer mode adds firmware, SDK and experimental tools. */
function ModeSwitch({ railed, tip }: { railed: boolean; tip: TipFactory }) {
  const { t, mode, setMode } = usePreferences();
  const developer = mode === 'developer';
  if (railed)
    return (
      <div className="mode-switch mode-switch--rail">
        <button
          type="button"
          className="icon-button"
          aria-pressed={developer}
          aria-label={t('Developer mode', 'Режим разработчика')}
          onClick={() => setMode(developer ? 'user' : 'developer')}
          {...tip(
            developer
              ? t('Developer mode: on', 'Режим разработчика: включён')
              : t('Developer mode: off', 'Режим разработчика: выключен'),
          )}
        >
          {developer ? (
            <Code2 size={18} aria-hidden="true" />
          ) : (
            <User size={18} aria-hidden="true" />
          )}
        </button>
      </div>
    );
  return (
    <div className="mode-switch" role="group" aria-label={t('Interface mode', 'Режим интерфейса')}>
      <button
        type="button"
        aria-pressed={!developer}
        data-mode="user"
        onClick={() => setMode('user')}
      >
        {t('User', 'Пользователь')}
      </button>
      <button
        type="button"
        aria-pressed={developer}
        data-mode="developer"
        onClick={() => setMode('developer')}
      >
        {t('Developer', 'Разработчик')}
      </button>
    </div>
  );
}

function SidebarFooter({
  railed,
  compact,
  onCompact,
  tip,
}: {
  railed: boolean;
  compact: boolean;
  onCompact: (value: boolean) => void;
  tip: TipFactory;
}) {
  const { t, language, theme, setLanguage, setTheme } = usePreferences();
  const api = useApi();
  const lan = authMode() === 'lan';
  const [panel, setPanel] = useState(false);
  const panelId = useId();
  const ThemeIcon = theme === 'dark' ? Moon : theme === 'light' ? Sun : SunMoon;
  const nextTheme = theme === 'system' ? 'light' : theme === 'light' ? 'dark' : 'system';
  const themeName = {
    system: t('System', 'Системная'),
    light: t('Light', 'Светлая'),
    dark: t('Dark', 'Тёмная'),
  }[theme];
  return (
    <div className="sidebar-footer">
      {panel && (
        <div
          className="settings-panel"
          id={panelId}
          role="group"
          aria-label={t('Settings', 'Настройки')}
        >
          <label htmlFor="language">{t('Language', 'Язык')}</label>
          <select
            id="language"
            value={language}
            onChange={(event) => setLanguage(event.target.value === 'en' ? 'en' : 'ru')}
          >
            <option value="ru">Русский</option>
            <option value="en">English</option>
          </select>
          {DARK_THEME_ENABLED && (
            <>
              <label htmlFor="theme">{t('Theme', 'Тема')}</label>
              <select
                id="theme"
                value={theme}
                onChange={(event) =>
                  setTheme(
                    event.target.value === 'dark'
                      ? 'dark'
                      : event.target.value === 'light'
                        ? 'light'
                        : 'system',
                  )
                }
              >
                <option value="system">{t('System', 'Системная')}</option>
                <option value="light">{t('Light', 'Светлая')}</option>
                <option value="dark">{t('Dark', 'Тёмная')}</option>
              </select>
            </>
          )}
          <label className="settings-switch">
            <input
              type="checkbox"
              checked={compact}
              onChange={(event) => onCompact(event.target.checked)}
            />
            {t('Compact menu (icons only)', 'Компактное меню (только значки)')}
          </label>
          <a className="settings-link" href="#phone/connection">
            <Layers size={16} aria-hidden="true" />
            {t('Connection settings', 'Настройки подключения')}
          </a>
        </div>
      )}
      <div className="footer-actions">
        <button
          type="button"
          className="icon-button"
          aria-expanded={panel}
          aria-controls={panel ? panelId : undefined}
          aria-label={t('Settings', 'Настройки')}
          onClick={() => setPanel((value) => !value)}
          {...tip(t('Settings', 'Настройки'))}
        >
          <Settings size={18} aria-hidden="true" />
        </button>
        {lan && (
          <button
            type="button"
            className="icon-button"
            aria-label={t('Sign out', 'Выйти')}
            onClick={() => void api.logout()}
            {...tip(t('Sign out', 'Выйти'))}
          >
            <LogOut size={18} aria-hidden="true" />
          </button>
        )}
        {DARK_THEME_ENABLED && (
          <button
            type="button"
            className="icon-button"
            aria-label={`${t('Theme', 'Тема')}: ${themeName}`}
            onClick={() => setTheme(nextTheme)}
            {...tip(`${t('Theme', 'Тема')}: ${themeName}`)}
          >
            <ThemeIcon size={18} aria-hidden="true" />
          </button>
        )}
        <button
          type="button"
          className="icon-button icon-button--text"
          aria-label={`${t('Language', 'Язык')}: ${language === 'ru' ? 'Русский' : 'English'}`}
          onClick={() => setLanguage(language === 'ru' ? 'en' : 'ru')}
          {...tip(`${t('Language', 'Язык')}: ${language === 'ru' ? 'Русский' : 'English'}`)}
        >
          <Languages size={18} aria-hidden="true" />
          {!railed && <span>{language.toUpperCase()}</span>}
        </button>
        {!railed && (
          <button
            type="button"
            className="icon-button"
            aria-label={t('Collapse menu to icons', 'Свернуть меню до значков')}
            onClick={() => onCompact(true)}
          >
            <PanelLeftClose size={18} aria-hidden="true" />
          </button>
        )}
        {railed && (
          <button
            type="button"
            className="icon-button"
            aria-label={t('Expand menu', 'Развернуть меню')}
            onClick={() => onCompact(false)}
            {...tip(t('Expand menu', 'Развернуть меню'))}
          >
            <PanelLeftOpen size={18} aria-hidden="true" />
          </button>
        )}
      </div>
    </div>
  );
}
