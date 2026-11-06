# Публичное развёртывание EduFlow

Эта инструкция рассчитана на **один Linux-сервер с Docker Engine/Compose и собственный домен**. Конфигурация подготовлена; сервер, домен и удалённое хранилище ещё не выбраны, поэтому публикация не выполнялась. Команды ниже выполняются через SSH на будущем сервере, а не в Windows PowerShell.

```text
Пользователь → HTTPS:443 → Caddy → FastAPI → PostgreSQL
                                      ↘ Redis → Celery → аудио + AI

pg_dump + аудио + код/.env → restic (шифрование) → отдельное хранилище
```

## 1. Что подготовить

- Linux VPS с постоянным публичным IP и SSH по ключу. Для этих команд подойдёт поддерживаемая Docker версия Ubuntu LTS. Нагрузка Faster-Whisper зависит от длительности лекций и модели; размер сервера подбирайте после замера обработки реального файла.
- Домен или поддомен, например `api.your-domain.ru`. У регистратора добавьте **A-запись** с IPv4 сервера. AAAA добавляйте только при работающем IPv6 на сервере. Это пример, реальное имя нужно подставить самостоятельно.
- Отдельное хранилище для резервных копий: S3-совместимый бакет в другом аккаунте/на другом сервере. Бакет должен быть приватным; это хранилище резервных копий, не публичная раздача аудио.

Установите Docker Engine и Compose plugin по [официальной инструкции Docker для Ubuntu](https://docs.docker.com/engine/install/ubuntu/). На Linux-сервере Docker Desktop не требуется. Проверьте:

```bash
docker version
docker compose version
```

Если вашему SSH-пользователю нужен `sudo docker`, используйте его во всех командах Docker ниже. Доступ к Docker фактически даёт административные права на сервер; не выдавайте его посторонним.

В сетевом экране провайдера разрешите входящие TCP **80 и 443** для всех и SSH-порт только со своих адресов. Если SSH настроен не на 22, разрешите реальный порт до изменения правил. PostgreSQL 5432, Redis 6379 и API 8000 наружу не открывайте: в production Compose у этих сервисов нет `ports`. Не полагайтесь только на UFW для опубликованных Docker-портов: см. [Docker и firewall](https://docs.docker.com/engine/network/packet-filtering-firewalls/).

## 2. Подготовить код и секреты

Разместите проверенную версию проекта в `/srv/eduflow/app`. В командном репозитории изменение проходит feature-ветку → PR в `develop` → Approved Руслана и QA; на сервер выкладывается согласованная версия. Прямые коммиты в `main`/`master` не нужны.

```bash
cd /srv/eduflow/app
cp .env.production.example .env.production
chmod 600 .env.production
```

Заполните `.env.production` редактором:

```dotenv
PUBLIC_DOMAIN=api.your-domain.ru
ACME_EMAIL=your-real-email@example.com
DATA_DIR=/srv/eduflow-data
```

Также задайте `POSTGRES_PASSWORD` и `JWT_SECRET` разными случайными значениями. Для каждого отдельно выполните `python3 -c 'import secrets; print(secrets.token_hex(32))'` и перенесите результат в файл на сервере. Пароль из hex-символов подходит для автоматически собираемого URL БД. Заполните выбранные `AI_PROVIDER`, `AI_MODEL` и соответствующий API-ключ. Без AI можно проверять инфраструктуру и авторизацию, но обработка лекций будет завершаться ошибкой.

Секреты приложение читает через pydantic-settings из окружения контейнера. Не коммитьте `.env.production` и `.env.backup`: `.gitignore` настроен. Передача серверу — через SSH/SCP или менеджер секретов, а не Telegram-чат/PR.

Подготовьте постоянные каталоги:

```bash
sudo install -d -m 700 /srv/eduflow-data
sudo install -d -m 700 -o 10001 -g 10001 /srv/eduflow-data/audio
sudo install -d -m 700 /srv/eduflow-data/postgres /srv/eduflow-data/redis
```

`10001` — UID/GID пользователя API/воркера в Dockerfile. PostgreSQL и Redis при первом старте назначают права своих каталогов через штатные entrypoint. При другом `DATA_DIR` замените пути и сохраните владельца audio. Не используйте `chmod 777`.

## 3. Запустить отдельную production-конфигурацию

Всегда указывайте **оба** параметра `--env-file` и `-f`. `compose.production.yaml` самостоятельный; объединять его с локальным `compose.yaml` не нужно.

```bash
cd /srv/eduflow/app
docker compose --env-file .env.production -f compose.production.yaml config --quiet
docker compose --env-file .env.production -f compose.production.yaml up -d --build
docker compose --env-file .env.production -f compose.production.yaml ps
docker compose --env-file .env.production -f compose.production.yaml logs --tail=100 caddy api worker
```

Первый build воркера может занять несколько минут. Миграции выполняет сервис `migrate`; API и воркер ждут его успешного завершения. Только Caddy публикует порты 80/443. API и воркер могут обращаться к AI, БД и Redis доступны лишь внутри Docker-сети.

Caddy получает сертификат для `PUBLIC_DOMAIN`, продлевает его и перенаправляет HTTP на HTTPS. Для этого DNS должен указывать на сервер, а 80/443 быть доступны извне. Сертификаты сохраняются в volume `caddy_data`. См. [Automatic HTTPS](https://caddyserver.com/docs/automatic-https).

В конфигурации зарезервирована сеть `172.30.80.0/24`; IP Caddy — `172.30.80.2`. Uvicorn доверяет proxy-заголовкам только от этого адреса. Если подсеть пересекается с сетью вашего VPS/VPN, измените `subnet`, `ipv4_address` Caddy и `--forwarded-allow-ips` API согласованно. Пока не подключайте CDN перед Caddy: для него потребуется отдельно настроить доверенные адреса и получение реального IP клиента.

Проверка с другого компьютера:

```bash
curl -I http://api.your-domain.ru/health
curl https://api.your-domain.ru/health
curl https://api.your-domain.ru/ready
```

Ожидается перенаправление HTTP и `{"status":"ok"}` / `{"status":"ready"}` по HTTPS. Браузер должен открывать `https://api.your-domain.ru/docs` с действующим сертификатом. Наличие HTTPS не означает наличие готового пользовательского интерфейса: проект по-прежнему предоставляет API и Swagger.

Если сертификат не получается, проверьте A/AAAA, firewall провайдера, занятость 80/443 и журнал `caddy`. Для экспериментов с сертификатами используйте тестовый ACME endpoint по документации Caddy, чтобы не упереться в ограничения выдачи.

## 4. Какие ограничения уже включены

| Что | Лимит по умолчанию | Ответ при превышении |
|---|---|---|
| Файл аудио в приложении | 100 МиБ | 413 |
| Весь multipart-запрос загрузки в Caddy | 101 МиБ | 413 |
| Тело остальных запросов в Caddy | 64 КиБ | 413 |
| Все auth-запросы вместе | 10 за 60 секунд с IP | 429 + Retry-After |
| Чат | 20 за 60 секунд на пользователя | 429 + Retry-After |
| Загрузки, запускающие платную обработку | 10 за час на пользователя | 429 + Retry-After |

Proxy-лимиты находятся в `deploy/Caddyfile`; предел файла — в `MAX_UPLOAD_BYTES`. Небольшой запас Caddy оставляет место multipart-заголовкам. JSON-лимит относится к запросу, а не размеру ответа с транскрипцией. Описание `request_body` — в [документации Caddy](https://caddyserver.com/docs/caddyfile/directives/request_body).

Частотные лимиты реализованы общими счётчиками Redis, поэтому работают и с несколькими процессами API. Окно начинается с первого запроса; возле границы окна возможны два последовательных разрешённых пакета. Auth-счётчик объединяет регистрацию, вход, refresh и logout для одного IP; пользователи одной сети делят этот лимит. Чат и загрузки считаются по UUID аккаунта, а не по JWT: смена access-токена не обнуляет счётчик. Входящие `X-Forwarded-For` от клиента Caddy заменяет, а Uvicorn принимает proxy-заголовки только от Caddy.

При недоступности Redis защищённые операции получают 503, а не обходят ограничение. Значения меняются в `.env.production`: `AUTH_REQUESTS_PER_MINUTE`, `CHAT_REQUESTS_PER_MINUTE`, `UPLOADS_PER_HOUR`. Production Compose принудительно включает `RATE_LIMIT_ENABLED=true`; локальная конфигурация по умолчанию оставляет его выключенным.

Это лимиты частоты, а не жёсткий денежный бюджет: длительные лекции требуют нескольких AI-вызовов, и один пользователь может создать несколько аккаунтов. Для платного сервиса дополнительно понадобятся подтверждение email/допуск пользователей, дневные квоты и контроль фактических расходов. Текущая конфигурация подходит как основа для ограниченного запуска.

## 5. Хранение файлов

Аудио хранится на постоянном диске сервера в `/srv/eduflow-data/audio`, монтируется в API/воркер как `/srv/eduflow/uploads`. UUID в имени исключает использование оригинального имени как пути. Caddy не раздаёт этот каталог; доступ к результатам проверяет API. Обновление контейнера не удаляет файлы.

Это вариант для одного сервера. При нескольких серверах потребуется приватное S3-совместимое хранилище и адаптер загрузки/чтения в приложении: просто прописать URL бакета вместо `UPLOAD_DIR` недостаточно. В текущей реализации модели сохраняют локальный путь, и воркер читает локальный файл.

Задайте оповещение о заполнении диска в мониторинге выбранного провайдера. Автоматического удаления старых аудио пока нет. Не удаляйте их вручную по дате, пока БД ссылается на файлы; политику хранения и удаление нужно реализовать согласованно с БД и резервным копированием.

## 6. Настроить резервные копии

Подготовлены `scripts/backup.sh` и systemd service/timer. Скрипт:

1. Делает `pg_dump --format=custom` и проверяет, что файл не пустой.
2. Сохраняет дамп с SHA-256, аудио и исходники/конфигурацию, включая `.env.production`, через restic в **зашифрованный** репозиторий.
3. Проверяет целостность метаданных репозитория.
4. Оставляет 7 дневных, 4 недельных и 6 месячных точек восстановления для EduFlow; удаляет лишние только после успешной проверки.
5. Удаляет локальный временный дамп. Другие данные не удаляет.

`pg_dump` даёт согласованный снимок одной БД: [PostgreSQL 16](https://www.postgresql.org/docs/16/app-pgdump.html). Аудио копируется после дампа; это корректно при текущем поведении проекта, где загруженные файлы не изменяются и не удаляются. В снимок могут попасть лишние, более новые файлы — это допустимо. При добавлении удаления/retention файлов порядок нужно пересмотреть. Живой каталог PostgreSQL не копируется как резервная копия БД.

Установите инструменты и конфигурацию:

```bash
sudo apt update
sudo apt install -y restic python3 util-linux
sudo install -d -m 700 /etc/eduflow /var/lib/eduflow-backup
sudo install -m 600 .env.backup.example /etc/eduflow/backup.env
sudoedit /etc/eduflow/backup.env
```

В `backup.env` заполните адрес репозитория и его отдельный случайный пароль, ключи бакета, регион, путь проекта. Используйте отдельные S3-учётные данные только для backup-бакета. **Сохраните RESTIC_PASSWORD отдельно в менеджере паролей**: после потери сервера одного бакета без пароля недостаточно. Секреты передавайте в переменных окружения, не в аргументах команд. Форматы репозиториев описаны в [Restic: preparing a repository](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html).

Один раз создайте репозиторий:

```bash
sudo bash -c 'set -a; . /etc/eduflow/backup.env; exec restic init'
```

Не используйте `.env.backup.example` с незаполненными значениями. Настоящий `backup.env` храните в `/etc/eduflow`, вне исходников и самого backup-набора. `.env.production` содержит секреты и попадает только в зашифрованный backup.

Установите расписание и сначала выполните пробную копию:

```bash
sudo install -m 644 deploy/systemd/eduflow-backup.service /etc/systemd/system/
sudo install -m 644 deploy/systemd/eduflow-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start eduflow-backup.service
sudo systemctl status eduflow-backup.service
sudo journalctl -u eduflow-backup.service -n 100 --no-pager
# Включайте расписание после успешной первой копии.
sudo systemctl enable --now eduflow-backup.timer
sudo systemctl list-timers eduflow-backup.timer
```

Копия запускается ежедневно примерно в **03:00–03:15 по Москве**, независимо от часового пояса сервера. При другом пути проекта измените `ExecStart` в `.service` и `EDUFLOW_PROJECT_DIR` в `backup.env`. В systemd задан абсолютный путь, чтобы задача не зависела от текущей папки. Настройте уведомление мониторинга о неуспешном `eduflow-backup.service` и возрасте последнего snapshot: успешная установка таймера не гарантирует успешность будущих копий.

Redis в backup не включён: после восстановления чистого Redis незавершённые задания публикуются заново по состоянию PostgreSQL. Частотные счётчики начнутся с нуля. Кэш Whisper можно скачать повторно; сертификаты Caddy при необходимости выдаются заново.

## 7. Проверить восстановление на отдельной машине

Не проверяйте восстановление поверх рабочей БД. Подготовьте отдельный тестовый сервер с пустыми данными и без DNS/публичного Caddy. Один новый каталог на рабочем Docker-хосте сам по себе не изолирует сети: фиксированная proxy-подсеть потребует отдельной настройки.

```bash
# На отдельной машине, с доступом к backup.env из менеджера секретов:
sudo bash -c 'set -a; . /etc/eduflow/backup.env; exec restic snapshots --host eduflow-production --tag eduflow'
# Подставьте конкретный SNAPSHOT_ID из предыдущей команды.
sudo bash -c 'set -a; . /etc/eduflow/backup.env; exec restic restore SNAPSHOT_ID --target /srv/eduflow-restore'
```

Restic сохраняет абсолютные пути внутри target. При путях из примера вы получите:

- `/srv/eduflow-restore/srv/eduflow/app` — код и `.env.production`;
- `/srv/eduflow-restore/srv/eduflow-data/audio` — аудио;
- `/srv/eduflow-restore/var/lib/eduflow-backup/current/database.dump` — дамп.

Если пути сервера были другими, посмотрите содержимое восстановленного каталога и используйте реальные. В копии `.env.production` установите **новый пустой DATA_DIR**, например `/srv/eduflow-restore-data`. Подготовьте его каталоги как в шаге 2, скопируйте восстановленное audio в новый `DATA_DIR/audio`, назначьте UID/GID `10001:10001` и права `700`. Не меняйте путь `/srv/eduflow/uploads` внутри контейнера: на него ссылаются восстановленные записи БД.

Сначала проверьте контрольную сумму дампа из его каталога:

```bash
cd /srv/eduflow-restore/var/lib/eduflow-backup/current
sha256sum -c database.dump.sha256
```

Из папки восстановленного приложения поднимите **только** пустые PostgreSQL и Redis. `migrate`, API и воркер до восстановления не запускайте:

```bash
cd /srv/eduflow-restore/srv/eduflow/app
docker compose --env-file .env.production -f compose.production.yaml -p eduflow-restore up -d --wait postgres redis
docker compose --env-file .env.production -f compose.production.yaml -p eduflow-restore exec -T postgres sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --exit-on-error --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < /srv/eduflow-restore/var/lib/eduflow-backup/current/database.dump
```

После успешного restore соберите API и воркер, примените ещё не выполненные миграции и запустите их. Caddy на тестовом восстановлении пока не запускается:

```bash
docker compose --env-file .env.production -f compose.production.yaml -p eduflow-restore build api worker
docker compose --env-file .env.production -f compose.production.yaml -p eduflow-restore run --rm --no-deps migrate
docker compose --env-file .env.production -f compose.production.yaml -p eduflow-restore up -d --no-deps api worker
docker compose --env-file .env.production -f compose.production.yaml -p eduflow-restore exec -T api python -m app.recovery
```

`app.recovery` повторно публикует только `pending`/`processing`; `completed` и `failed` не трогает. Это может снова вызвать AI для незавершённых лекций — используйте тестовые ключи и контролируйте расходы. Перед полноценным тестом проверьте наличие нужных аудиофайлов. Повторная доставка защищена блокировкой по лекции и проверкой `completed`.

Проверьте `/ready` из контейнера, количество пользователей/лекций, возможность входа старым паролем, результаты завершённой лекции и наличие её аудио. Возвращайте публичный трафик только после успешной проверки. Планируйте проверку восстановления регулярно и после изменений схемы/backup-скрипта. Текущий дневной график означает до суток потерянных новых данных при аварии; если это неприемлемо, потребуются более частые копии и PostgreSQL PITR.

## 8. Обновления и текущие границы

Перед обновлением делайте backup и проверяйте совместимость миграций. Для простого односерверного развёртывания запланируйте окно обслуживания: остановите `caddy`, `api`, `worker` (воркер получает время закончить задачу), соберите согласованную версию, выполните `run --rm migrate`, затем `up -d`. Следите за `ps`, `/ready` и журналом воркера. Не удаляйте тома/каталоги данных ради пересборки. Перед релизом зафиксируйте протестированные версии/дигесты образов и зависимостей; повторная сборка по плавающим тегам может обновить зависимости.

Подготовленные файлы закрывают HTTPS, ограничения запросов, базовые лимиты расходов, постоянное хранение на одном сервере и процедуру off-server backup/restore. Они не заменяют нагрузочный тест, QA и review. Реальный сертификат, доставка backup в ваш бакет, работа уведомлений и полный restore проверяются после выбора сервера/домена/хранилища. Публичная регистрация и текущая модель затрат требуют отдельной приёмки перед открытым платным запуском.
