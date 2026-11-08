# Установка одним Compose-файлом (Portainer, UGREEN, Docker Compose)

Клонировать репозиторий и собирать образ не нужно: [`compose.deploy.yaml`](../compose.deploy.yaml) использует готовый образ
**`ghcr.io/grayhex/s60modern`** и не содержит `build:` и путей к файлам репозитория.

## 1. Что публикуется

Workflow [`publish-image.yml`](../.github/workflows/publish-image.yml) собирает и публикует образ:

| Событие | Теги |
|---|---|
| push в `main` | `latest`, `main`, `sha-<коммит>` |
| тег `X.Y.Z` или `vX.Y.Z` | `X.Y.Z`, `X.Y` (`latest` следует за `main`, а не за тегом) |

**Архитектуры.** Каждая собирается на своём «родном» раннере и **публикуется только после smoke-теста запущенного контейнера**
(HTTP API, главная страница, пользователь 10001, контракт Gammu, сборка темы): `linux/amd64` обязательна, `linux/arm64`
(`ubuntu-24.04-arm`) публикуется, только если сборка и тот же тест прошли. Манифест перечисляет ровно то, что прошло; проверить:
`docker buildx imagetools inspect ghcr.io/grayhex/s60modern:latest`. Эмулятор (профиль `emulator`) — только amd64.

> Публикация идёт при каждом push в `main`. Если пакет в GHCR оказался приватным, откройте *Packages → s60modern →
> Package settings → Change visibility → Public*, иначе Portainer/UGREEN потребуют реквизиты реестра (логин GitHub
> и PAT с `read:packages`). Установка на Proxmox: [DEPLOY_PROXMOX.md](DEPLOY_PROXMOX.md).

## 2. Установка

**Portainer.** Stacks → *Add stack* → имя `symport` → *Web editor* → вставьте содержимое
`https://raw.githubusercontent.com/grayhex/s60modern/main/compose.deploy.yaml` → при необходимости задайте переменные
(*Environment variables*) → *Deploy the stack*.

**UGREEN (Docker → Проект → Создать).** Имя `symport`, вставьте тот же YAML в редактор Compose (или загрузите файл) → запустить.
Переменные окружения задаются в разделе переменных проекта (или заменой значений `${…:-…}` прямо в файле).

**Командная строка:**

```bash
curl -fsSLO https://raw.githubusercontent.com/grayhex/s60modern/main/compose.deploy.yaml
docker compose -f compose.deploy.yaml up -d
docker compose -f compose.deploy.yaml ps        # healthy через ~20 с
```

Данные (копии телефона, отчёты, загрузки) лежат в именованных томах `symport-build` и `symport-workspace`.

## 3. Два режима доступа

| | **Localhost** (по умолчанию) | **Home LAN** (включается явно) |
|---|---|---|
| Адрес | `http://127.0.0.1:8765` на самом хосте | `http://<IP-NAS>:8765` с другого компьютера |
| Вход | SSH-туннель или HTTPS-прокси с аутентификацией | встроенный экран входа (пароль администратора) |
| Порт на хосте | только loopback | один конкретный адрес NAS из `SYMPORT_BIND` |
| Что нужно задать | ничего | `SYMPORT_ACCESS_MODE=lan`, `SYMPORT_BIND`, `SYMPORT_ADMIN_PASSWORD` |

**Localhost.** Сервер принимает только Host `127.0.0.1:<порт>` / `localhost:<порт>` либо точный HTTPS-адрес из
`SYMPORT_PUBLIC_ORIGIN`. Поэтому `http://IP-NAS:8765` без включения Home LAN **отклоняется (403, Host not allowed)** — это
защита, а не ошибка:

* **SSH-туннель:** `ssh -L 8765:127.0.0.1:8765 user@nas`, затем `http://127.0.0.1:8765` на своём компьютере.
* **HTTPS-прокси** (встроенный в NAS или отдельный) с **обязательной аутентификацией**: задайте
  `SYMPORT_PUBLIC_ORIGIN=https://symport.example` (точный адрес) и проксируйте на `127.0.0.1:8765`. Требования к прокси —
  в [DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md#windows-phone-connection). `SYMPORT_PUBLIC_ORIGIN` — это список разрешённых
  адресов, а **не** аутентификация.

## 4. UGREEN / Portainer: Home LAN без SSH и прокси

Для домашнего NAS: открыть SymPort по IP из локальной сети и войти паролем.

1. Создайте проект/стек `symport` и вставьте содержимое [`compose.deploy.yaml`](../compose.deploy.yaml) (раздел 2).
2. В переменных проекта (Portainer: *Environment variables*; UGREEN: переменные проекта) задайте **три** значения
   (адрес и пароль ниже — пример, подставьте свои):

   ```env
   SYMPORT_ACCESS_MODE=lan
   SYMPORT_BIND=192.168.1.5
   SYMPORT_ADMIN_PASSWORD=<ваш-собственный-пароль-не-короче-12-символов>
   ```

   * `SYMPORT_BIND` — **IP вашего NAS** в локальной сети (пример `192.168.1.5` ни во что не зашит). Порт публикуется
     только на этом адресе, а сервер принимает только Host/Origin ровно `http://192.168.1.5:8765`.
   * `SYMPORT_ADMIN_PASSWORD` — ваш пароль. Пустой, короткий (меньше 12 символов), типовой («password», «changeme») или
     похожий на заготовку пароль **не принимается**. Вместо пароля можно задать `SYMPORT_ADMIN_PASSWORD_FILE`
     (путь к файлу внутри контейнера, том смонтируйте только для чтения) или готовый хеш
     `SYMPORT_ADMIN_PASSWORD_HASH` (получить: `echo -n 'пароль' | docker run --rm -i ghcr.io/grayhex/s60modern python -m symport.access hash`).
   * Нужно имя вместо IP (`nas.home`)? Добавьте `SYMPORT_LAN_HOST=nas.home` — это ещё один **точный** разрешённый адрес.
     Если NAS получает адрес по DHCP, закрепите IP в роутере: при смене IP измените `SYMPORT_BIND` и перезапустите стек.
3. Запустите стек. С другого компьютера откройте `http://192.168.1.5:8765/` — появится экран входа. Войдите; кнопка
   «Выйти» — в нижней панели меню. Ничего клонировать и настраивать по SSH не нужно.

**Если что-то не так, контейнер не стартует и сообщает причину** (логи стека, код выхода 78): нет пароля, пароль слабый,
`SYMPORT_BIND` — `0.0.0.0`/`127.0.0.1` либо не совпадает с портом, неизвестный режим. SymPort никогда не откроет API без
защиты «в обход». `SYMPORT_BIND=0.0.0.0` сам по себе ничего не открывает и ничего не отключает: в Localhost-режиме это
ошибка конфигурации, а в Home LAN он допустим только вместе с точным `SYMPORT_LAN_HOST`.

### Как устроена защита Home LAN

* Любой запрос, кроме экрана входа и `/healthz`, требует входа: файлы, контакты, копии, медиа (в т. ч. Range), каталог,
  диагностика и все действия без сессии получают `401`.
* Пароль не хранится и не передаётся в URL; на сервере — scrypt-хеш. Серверная сессия живёт `SYMPORT_SESSION_IDLE_MINUTES`
  (120 мин без активности) и не дольше `SYMPORT_SESSION_MAX_MINUTES` (12 ч); выход аннулирует её на сервере; перезапуск
  контейнера выводит всех.
* Cookie `HttpOnly`, `SameSite=Strict` (и `Secure` за HTTPS-прокси). Операции записи требуют CSRF-токен сессии и Origin
  точно из списка; ограничение попыток входа: после 5 неверных с одного адреса (30 всего) — пауза 5 минут (`429`).
* Windows Bridge работает как раньше: `/api/bridge-relay/*` защищены собственным короткоживущим ключом и не обходят вход
  для остальных операций; старый ключ `SYMPORT_BRIDGE_KEY` в Home LAN не даёт доступа к API.

### Важно: это обычный HTTP

Без TLS пароль, cookie сессии и данные телефона **видны любому, кто может перехватывать трафик в вашей сети** (общий Wi‑Fi,
гостевая сеть, скомпрометированное устройство). Вход по обычному HTTP — осознанный выбор для доверенной домашней сети; при
запуске в логе печатается предупреждение. Для защиты используйте HTTPS-прокси: `SYMPORT_PUBLIC_ORIGIN=https://…` — вход,
сессия и `Secure` cookie работают и там.

### Не открывать в WAN

* **Не пробрасывайте порт 8765 на роутере** (port forward, DMZ, UPnP) и не публикуйте его через DDNS/«облачный» доступ NAS.
  Из интернета SymPort надо открывать только через VPN или HTTPS-прокси с отдельной аутентификацией.
* Home LAN не включает IPv6/WAN-доступ сам: порт публикуется на одном адресе из `SYMPORT_BIND`.

### Смена IP, возврат и забытый пароль

* **Сменился IP NAS:** обновите `SYMPORT_BIND` (и `SYMPORT_LAN_HOST`, если задан), перезапустите стек. Старый адрес перестанет работать.
* **Вернуться в Localhost:** удалите `SYMPORT_ACCESS_MODE`, `SYMPORT_BIND`, `SYMPORT_ADMIN_PASSWORD*` (или задайте
  `SYMPORT_ACCESS_MODE=localhost`, `SYMPORT_BIND=127.0.0.1`) и перезапустите. Тома с данными не затрагиваются.
* **Забыли пароль:** пароль лежит только в вашей переменной/файле. Задайте новый `SYMPORT_ADMIN_PASSWORD` и перезапустите
  стек — старые сессии сбрасываются, данные в томах остаются.
* Проверка до запуска: `docker compose -f compose.deploy.yaml config` показывает итоговые значения.

## 5. Настройки (переменные)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `SYMPORT_IMAGE` | `ghcr.io/grayhex/s60modern:latest` | образ или тег (например `…:0.10.0`; лучше закрепить версию) |
| `SYMPORT_HTTP_PORT` | `8765` | порт (меняется вместе с проброшенным портом и healthcheck) |
| `SYMPORT_ACCESS_MODE` | `localhost` | `lan` — Home LAN с обязательным входом (см. раздел 4) |
| `SYMPORT_BIND` | `127.0.0.1` | адрес публикации порта на хосте; в Home LAN — IP вашего NAS |
| `SYMPORT_LAN_HOST` | пусто | дополнительный точный адрес/имя для Home LAN (обязателен при `SYMPORT_BIND=0.0.0.0`) |
| `SYMPORT_ADMIN_PASSWORD`, `…_FILE`, `…_HASH` | пусто | пароль администратора (ровно один способ); обязателен в Home LAN |
| `SYMPORT_SESSION_IDLE_MINUTES`, `SYMPORT_SESSION_MAX_MINUTES` | 120 / 720 | простой и предельная длительность сессии |
| `SYMPORT_PUBLIC_ORIGIN` | пусто | точный HTTPS-адрес прокси |
| `SYMPORT_DEVICE_ROOTS` | `{}` | явно подключённые папки телефона/карты, например `{"phone":"/phone"}` |
| `SYMPORT_PULL_POLICY` | `missing` | `always` — тянуть образ при каждом запуске |
| `SYMPORT_UI_MODE` | пусто (`user`) | `developer` — свежий браузер стартует в режиме разработчика (выбор пользователя важнее) |
| `SYMPORT_GAMMU_CONFIG`, `SYMPORT_BRIDGE_*` | пусто | Gammu и PC Bridge ([DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md)) |

Переменные хранятся в стеке (Portainer/UGREEN) и сохраняются при обновлении образа. Папка карты телефона: раскомментируйте
в файле строку тома `- /volume1/phone:/phone:ro` (укажите свой путь, не корень хоста) и задайте
`SYMPORT_DEVICE_ROOTS='{"phone":"/phone"}'`. Запись на телефон при `:ro` невозможна — уберите `:ro`, если нужна запись.

## 6. Обновление и откат

* Portainer: Stacks → `symport` → *Pull and redeploy* (или *Re-pull image*); CLI: `docker compose -f compose.deploy.yaml pull && … up -d`.
* Откат: задайте `SYMPORT_IMAGE=ghcr.io/grayhex/s60modern:X.Y.Z` (предыдущий тег) и перезапустите стек. Тома сохраняются.
* `docker compose down -v` удалит данные — не используйте без копии.

## 7. Ограничения

Вход в Home LAN проверен автотестами и в Docker-smoke CI, но не на конкретном NAS UGREEN. Pairing Windows Bridge со страницы по LAN-адресу зависит от того, пропускает ли браузер запросы к локальному агенту (`127.0.0.1:24652`) с частного адреса; на реальных браузерах это не проверялось. USB-телефон, подключённый к другому компьютеру, виден только через
Windows Bridge ([WINDOWS_BRIDGE.md](WINDOWS_BRIDGE.md)); контейнер на NAS не получает USB автоматически.
