# SymPort на Proxmox VE

SymPort — один контейнер Docker (`ghcr.io/grayhex/s60modern`), поэтому на Proxmox его запускают внутри
**LXC-контейнера с Docker** (лёгкий вариант) или **виртуальной машины** (строже изоляция, USB-проброс).
Телефон подключается через [Windows Bridge](WINDOWS_BRIDGE.md) исходящим соединением, так что USB на сам
Proxmox пробрасывать не нужно.

| | LXC (рекомендуется) | VM |
|---|---|---|
| Ресурсы | 2 vCPU, 2 ГиБ RAM, 12 ГиБ диска | 2 vCPU, 2–3 ГиБ RAM, 16 ГиБ диска |
| Установка | `tools/proxmox/create-lxc.sh` | Debian 12/13 + Docker, затем [compose.deploy.yaml](../compose.deploy.yaml) |
| Прямой USB-проброс телефона | неудобен | `qm set <vmid> -usb0 host=<vid>:<pid>` |
| Эмулятор (профиль `emulator`) | нужны права Docker внутри LXC, расход памяти | проще |

Эмулятор Lab требует 3 ГиБ памяти под собственный контейнер: для него берите VM или увеличьте `--memory`.

## Вариант 1. LXC одной командой

На узле Proxmox от root:

```bash
curl -fsSLO https://raw.githubusercontent.com/grayhex/s60modern/main/tools/proxmox/create-lxc.sh
bash create-lxc.sh --dry-run            # сначала посмотреть все команды
bash create-lxc.sh                      # создать и запустить
```

Параметры: `--ctid`, `--hostname`, `--storage` (по умолчанию `local-lvm`), `--template-storage` (`local`),
`--bridge` (`vmbr0`), `--memory` (2048 МиБ), `--cores` (2), `--disk` (12 ГиБ), `--image` (тег образа).
Пример: `bash create-lxc.sh --ctid 120 --storage local-zfs --bridge vmbr1 --memory 3072`.

Что делает скрипт:

1. Скачивает шаблон Debian 12 (`pveam`).
2. Создаёт **непривилегированный** контейнер с `nesting=1,keyctl=1` — это то, что нужно Docker внутри LXC;
   включает автозапуск при старте узла.
3. Ставит Docker Engine и Compose из репозитория Docker.
4. Кладёт `compose.deploy.yaml` в `/opt/symport` и запускает `docker compose up -d`.

Скрипт записывает в `/opt/symport/.env` `SYMPORT_ACCESS_MODE=localhost`, поэтому порт слушает только **loopback контейнера** (`127.0.0.1:8765`): ничего не открыто в локальную сеть.

### Открыть интерфейс

SymPort принимает только Host `127.0.0.1:<порт>` / `localhost:<порт>` либо точный HTTPS-адрес из
`SYMPORT_PUBLIC_ORIGIN`, поэтому `http://IP-контейнера:8765` в браузере отклоняется — это защита, а не ошибка.

* **SSH-туннель** (проще всего): добавьте свой ключ в контейнер (`pct enter <CTID>`, `~/.ssh/authorized_keys`), затем
  на своём компьютере `ssh -L 8765:127.0.0.1:8765 root@<ip-контейнера>` и откройте `http://127.0.0.1:8765`.
* **HTTPS-прокси с аутентификацией** (Nginx Proxy Manager, Caddy, Traefik) на Proxmox или рядом: в `/opt/symport/.env`
  задайте `SYMPORT_PUBLIC_ORIGIN=https://symport.example`, `SYMPORT_BIND=0.0.0.0` (только если прокси в другом
  контейнере/хосте и порт закрыт файрволом Proxmox от остальной сети), пересоздайте стек. Требования к прокси
  (длинные запросы, исключения для relay Windows Bridge) — в
  [DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md#windows-phone-connection). Публичный адрес не заменяет аутентификацию.

### Обновление, откат, логи

```bash
pct exec <CTID> -- bash -c 'cd /opt/symport && docker compose pull && docker compose up -d'   # обновить
pct exec <CTID> -- docker compose -f /opt/symport/compose.yaml logs -f                       # логи
```

Откат: в `/opt/symport/.env` задайте `SYMPORT_IMAGE=ghcr.io/grayhex/s60modern:X.Y.Z` и повторите `up -d`. Данные
лежат в томах Docker `symport-build` и `symport-workspace` внутри контейнера. Перед обновлением делайте
**бэкап контейнера** средствами Proxmox (`vzdump`), `docker compose down -v` данные удаляет.

## Вариант 2. Виртуальная машина

1. Создайте VM Debian 12/13 (cloud-image или установка с ISO), 2 vCPU, 2–3 ГиБ, диск 16 ГиБ, VirtIO.
2. Установите Docker Engine и Compose по [инструкции Docker](https://docs.docker.com/engine/install/debian/).
3. Выполните шаги Portainer/CLI из [DEPLOY_PORTAINER.md](DEPLOY_PORTAINER.md): `compose.deploy.yaml`, `docker compose up -d`.
4. Если телефон подключён к самому Proxmox, пробросьте USB в VM: `lsusb` на узле → `qm set <vmid> -usb0 host=<vid>:<pid>`.
   Доступ к USB внутри Docker — по правилам [DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md) (без привилегированного режима).

## Если Docker в LXC не стартует

* Убедитесь, что контейнер создан с `nesting=1,keyctl=1` (`pct config <CTID>` → `features`).
* Контейнер должен быть непривилегированным на современном ядре с cgroup v2; на очень старых версиях Proxmox
  Docker в LXC работает хуже — используйте VM.
* Файловая система хранилища: для `overlay2` внутри LXC лучше `local-lvm` или `zfs`; на `dir` с некоторыми
  файловыми системами Docker переходит на `vfs`, что медленно и расходует диск.

## Что проверено

Скрипт `tools/proxmox/create-lxc.sh` покрыт автотестами режима `--dry-run` (аргументы, непривилегированный
контейнер, `nesting`/`keyctl`, отсутствие открытых портов). Сам образ собирается и публикуется CI. Запуск на
конкретном узле Proxmox зависит от версии PVE и хранилища — при расхождении выводите команды через `--dry-run`
и повторяйте шаги вручную.


**Home LAN вместо SSH-туннеля.** В `/opt/symport/.env` задайте `SYMPORT_ACCESS_MODE=lan`, `SYMPORT_BIND=<IP контейнера>` и свой `SYMPORT_ADMIN_PASSWORD`, затем `docker compose up -d`: появится экран входа на `http://<IP>:8765/` (см. [DEPLOY_PORTAINER.md](DEPLOY_PORTAINER.md#4-ugreen--portainer-home-lan-без-ssh-и-прокси)).
