(function () {
  'use strict';
  var T = {
    ru: {
      lead: 'Войдите, чтобы открыть рабочее место SymPort.', password: 'Пароль администратора', submit: 'Войти',
      hint: 'Пароль задан в настройках запуска (SYMPORT_ADMIN_PASSWORD) на вашем NAS или компьютере.',
      wrong: 'Неверный пароль.', empty: 'Введите пароль.', locked: 'Слишком много попыток. Повторите через {n} с.',
      forbidden: 'Доступ запрещён: этот адрес не разрешён настройкой SymPort. Откройте SymPort по адресу, указанному в SYMPORT_BIND / SYMPORT_LAN_HOST.',
      offline: 'Нет связи с сервером SymPort. Проверьте, что NAS включён и вы в той же сети.',
      server: 'Сервер SymPort вернул ошибку. Попробуйте ещё раз.'
    },
    en: {
      lead: 'Sign in to open the SymPort workspace.', password: 'Administrator password', submit: 'Sign in',
      hint: 'The password is set in the launch settings (SYMPORT_ADMIN_PASSWORD) on your NAS or computer.',
      wrong: 'Wrong password.', empty: 'Enter the password.', locked: 'Too many attempts. Try again in {n} s.',
      forbidden: 'Access denied: this address is not allowed by the SymPort configuration. Open SymPort at the address set in SYMPORT_BIND / SYMPORT_LAN_HOST.',
      offline: 'Cannot reach the SymPort server. Check that the NAS is on and you are on the same network.',
      server: 'The SymPort server returned an error. Try again.'
    }
  };
  var lang = 'ru';
  try { lang = localStorage.getItem('symport-lang') || ''; } catch (e) { lang = ''; }
  if (lang !== 'ru' && lang !== 'en') lang = /^ru|^uk|^be/i.test(navigator.language || '') ? 'ru' : 'en';
  var form = document.getElementById('form'), input = document.getElementById('password');
  var submit = document.getElementById('submit'), message = document.getElementById('message');
  var toggle = document.getElementById('lang');
  function show(key, n) {
    message.hidden = false;
    message.textContent = T[lang][key].replace('{n}', String(n || ''));
  }
  function apply() {
    document.documentElement.lang = lang;
    toggle.textContent = lang === 'ru' ? 'EN' : 'RU';
    document.querySelectorAll('[data-i18n]').forEach(function (el) { el.textContent = T[lang][el.getAttribute('data-i18n')]; });
    message.hidden = true;
  }
  toggle.addEventListener('click', function () {
    lang = lang === 'ru' ? 'en' : 'ru';
    try { localStorage.setItem('symport-lang', lang); } catch (e) { /* storage may be blocked */ }
    apply();
  });
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    if (!input.value) { show('empty'); input.focus(); return; }
    submit.disabled = true;
    message.hidden = true;
    fetch('/api/auth/login', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: input.value })
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (response.ok) { input.value = ''; location.replace('/'); return; }
        if (response.status === 401) show('wrong');
        else if (response.status === 429) show('locked', body.retry_after || 60);
        else if (response.status === 403) show('forbidden');
        else show('server');
        input.select();
      });
    }).catch(function () { show('offline'); }).then(function () { submit.disabled = false; });
  });
  apply();
})();
