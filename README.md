# EduFlow API

Бэкенд для загрузки аудиолекций, распознавания речи, создания конспектов и карточек, вопросов AI-тьютору. Python 3.11+, FastAPI async, SQLAlchemy 2.0 async, PostgreSQL, Alembic, Redis, Celery, Faster-Whisper; OpenAI или DeepSeek выбирается через окружение.

Правила команды и пошаговая работа с Git: [CONTRIBUTING.md](CONTRIBUTING.md).

Публичное развёртывание, HTTPS, лимиты и резервное копирование: [DEPLOYMENT.md](DEPLOYMENT.md).

## Запуск через Docker Compose

Нужны Docker с Compose и Python 3.11+ для создания локального `.env`.

```powershell
python scripts/setup_env.py
```

В этой рабочей папке для проверок также подготовлен переносной Python. Если обычный `python` не установлен, команда создания конфигурации:

```powershell
.\.tools\python\python.exe scripts\setup_env.py
```

Скрипт создаёт `.env` из `.env.example`, генерирует случайные JWT-секрет и пароль БД, не печатает их и не перезаписывает существующий файл. Альтернатива — скопировать `.env.example` в `.env` и заполнить значения вручную. `.env` не должен попадать в Git.

В `.env` укажите `AI_PROVIDER=openai` и `OPENAI_API_KEY` либо `AI_PROVIDER=deepseek` и `DEEPSEEK_API_KEY`. Заполните `AI_MODEL` именем модели, доступной в вашем аккаунте и поддерживающей Chat Completions и JSON mode. При смене провайдера меняйте также модель. Модель специально не выбрана автоматически.

```powershell
docker compose up --build -d
docker compose logs -f api worker
```

Compose запускает PostgreSQL и Redis, применяет миграции отдельным сервисом `migrate`, после чего запускает API и воркер. Секреты сервисы получают через окружение. Встроенных рабочих паролей нет. Для `POSTGRES_PASSWORD` в Compose используйте сгенерированный скриптом пароль из шестнадцатеричных символов: ручной пароль со спецсимволами нужно URL-кодировать в `DATABASE_URL` и отдельно адаптировать Compose.

- Swagger: [http://localhost:8000/docs](http://localhost:8000/docs).
- OpenAPI: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json).
- Liveness: `/health`; readiness БД: `/ready`.

Остановка с сохранением данных: `docker compose down`. Аудио, база, Redis и кэш моделей находятся в именованных volumes. Первая обработка скачивает модель Faster-Whisper и требует доступа в интернет. По умолчанию воркер использует CPU/int8; GPU требует отдельного CUDA-образа и настройки окружения.

## Проверка через Swagger

1. `POST /api/v1/auth/register`: JSON `{"email":"student@example.com","password":"YourLongPassword123!"}`. Ответ `201`.
2. Кнопка **Authorize**: в `username` введите email, в `password` — пароль. Поле `username` используется по стандарту OAuth2; логин принимает form-urlencoded, регистрация — JSON.
3. `POST /api/v1/lectures/upload`: выберите mp3, wav или m4a. Ответ `202` содержит `id` и статус `pending`.
4. `GET /api/v1/lectures/{file_id}`: опрашивайте статус. После `completed` ответ содержит транскрипцию, таймкоды, Markdown-конспект, ключевые мысли и карточки.
5. `POST /api/v1/lectures/{file_id}/chat`: JSON `{"question":"Объясни основную идею лекции"}`.

`POST /api/v1/auth/login` возвращает access и refresh JWT. Дополнительно реализованы `/api/v1/auth/refresh` и `/api/v1/auth/logout`, оба принимают `{"refresh_token":"..."}`. Refresh при обновлении используется только один раз. Logout отзывает переданный refresh; уже выданный access действует до истечения срока (по умолчанию 15 минут).

## Обработка лекции и данные

```text
upload → файл с UUID → запись pending → Redis → Celery worker
       → processing → ffprobe → Faster-Whisper → AI → completed
                                                    ↘ failed
```

Пять основных сущностей соответствуют ТЗ: `users`, `audio_files`, `transcriptions`, `summaries`, `flashcards`. Дополнительно `refresh_sessions` хранит идентификаторы и сроки refresh-сессий для ротации и отзыва. Связи имеют внешние ключи; на одну лекцию допускается по одной транскрипции и конспекту. JSON в PostgreSQL хранится как JSONB. К аудио добавлены `created_at` и безопасное описание ошибки.

API проверяет владельца лекции; чужой идентификатор даёт `404`. Исходные пути файлов не возвращаются. Пароли хешируются Argon2. JWT проверяются по подписи, сроку, issuer, audience и типу. Некорректные запросы не отражают пароль или токен в теле ошибки валидации.

Загрузка ограничена 100 МиБ по умолчанию. Воркер проверяет реальный контейнер, наличие аудио и длительность (до 4 часов), поэтому поддельный mp3 или повреждённый файл принимается в очередь, затем получает `failed`. `clean_text` нормализует пробелы исходной транскрипции, не переписывая содержание нейросетью. Таймкоды сохраняются из Faster-Whisper.

Длинная транскрипция разбивается на части целиком, затем конспекты объединяются по группам. Невалидный, пустой или обрезанный ответ AI считается ошибкой. Чат ищет подходящие фрагменты по словам среди всей лекции и отправляет до четырёх фрагментов. Это простой поиск без embeddings; перефразированный вопрос может потребовать улучшения поиска. Каждый вопрос независим, история диалога не хранится.

Результаты и `completed` фиксируются одной транзакцией. PostgreSQL advisory lock препятствует одновременной обработке дубликатов; завершённая задача пропускается при повторной доставке. Celery подтверждает задачу после обработки; Redis сохраняется через AOF. Это не полноценный transactional outbox: авария API между фиксацией записи и отправкой в Redis может оставить `pending`. При штатной ошибке очереди API возвращает `503`, запись получает `failed`. Автоматического повторного запуска `failed` нет; после устранения причины загрузите файл заново. Очистка старых аудио и refresh-сессий пока выполняется администратором.

## Локальная разработка

Для API и тестов достаточно обычного Python. Для настоящего воркера нужны FFmpeg/ffprobe, зависимости `[worker]`, Redis и PostgreSQL; Celery запускайте в Linux/WSL2 или Docker.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
python scripts/setup_env.py
docker compose up -d postgres redis
alembic upgrade head
uvicorn app.main:app --reload
```

В локальном `.env` `DATABASE_URL` и `REDIS_URL` используют `localhost`; внутри Compose они автоматически заменяются на `postgres` и `redis`. Для отдельного воркера в Linux/WSL установите `python -m pip install '.[worker]'` и запустите `celery -A app.worker:celery_app worker --loglevel=INFO --concurrency=1`.

## Проверки и миграции

```powershell
python -m ruff check .
python -m ruff format --check .
python -m mypy app
python -m pytest -q
```

Тесты API и обработки работают в изолированной SQLite БД с подменой AI/очереди. Тесты PostgreSQL включаются через `TEST_DATABASE_URL`; используйте отдельную тестовую БД. Они создают временные схемы и проверяют конкурентную ротацию refresh и advisory locks. `TEST_REDIS_URL` включает проверку атомарного счётчика лимитов на настоящем Redis. CI поднимает PostgreSQL 16 и Redis 7, применяет миграции, проверяет расхождение моделей, запускает проверки и цикл downgrade/upgrade. Реальные AI-вызовы и распознавание в unit-тестах не запускаются.

Переносной Python в этой папке позволяет выполнить те же команды с префиксом `.\.tools\python\python.exe`; для тестов в ограниченном окружении используйте `-m pytest -q --basetemp=.tools/pytest-tmp`.

При изменении моделей:

```powershell
alembic revision --autogenerate -m "describe schema change"
# Прочитать сгенерированную миграцию; проверить обе стороны на тестовой БД.
alembic upgrade head
alembic check
```

Миграции — часть PR. Не меняйте уже применённую общую миграцию; создайте следующую. `Base.metadata.create_all` используется только в тестах.

## Границы готовности

Подготовлен исходный бэкенд и автоматические проверки. До приёмки нужны запуск всего Compose с настоящим аудио и выбранным AI-провайдером, QA и review Руслана. Интерфейс и Figma-макеты не предоставлены: Design Review для задачи только на API должен быть согласован как N/A, а не отмечен автоматически пройденным.

Для публичного развёртывания подготовлены отдельный `compose.production.yaml`, Caddy для HTTPS/лимитов размера, Redis-лимиты запросов и скрипт зашифрованного backup с systemd-таймером. Пошаговый запуск и проверка восстановления описаны в [DEPLOYMENT.md](DEPLOYMENT.md). Локальный `compose.yaml` публикует сервисы только на localhost; production-конфигурация наружу публикует только Caddy. Реальные DNS, сертификаты и удалённый backup проверяются на выбранном сервере.

## Официальная документация

- [FastAPI: OAuth2/JWT](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/).
- [SQLAlchemy 2.0 AsyncIO](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html).
- [Celery: задачи и подтверждение доставки](https://docs.celeryq.dev/en/stable/userguide/tasks.html).
- [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper).
- [OpenAI Python SDK](https://developers.openai.com/api/reference/python) и [JSON mode](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat).
- [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/).
