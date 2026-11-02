# Cloud Video Processor (CVP)

Розподілений хмарний сервіс пакетної обробки відео із високою доступністю, динамічним автомасштабуванням та моніторингом у реальному часі.

Побудовано на базі **FastAPI**, **Nginx** (балансувальник `least_conn` + HTTPS), **Dispatcher** (push-балансувальник задач), **Worker Server** (автономні HTTP-сервери обробки), **Redis** та **PostgreSQL**. Обробка мультимедіа здійснюється асинхронно через **FFmpeg** із трансляцією живого прогресу в браузер за допомогою **Server-Sent Events (SSE)**.

---

## 📐 Архітектурна схема

Детальний опис компонентів, життєвого циклу задачі та архітектурних рішень наведено у [docs/architecture.md](docs/architecture.md).

```mermaid
flowchart TB
    Browser["Браузер (SPA React + TS)<br/>SSE-прогрес, адмін-панель"]
    Nginx["Nginx Reverse Proxy<br/>HTTPS (TLS 1.3) + least_conn + Rate Limiting"]

    subgraph API_CLUSTER["Кластер API (Stateless)"]
        API1["api1 (FastAPI, Uvicorn)<br/>X-Served-By: api1"]
        API2["api2 (FastAPI, Uvicorn)<br/>X-Served-By: api2"]
    end

    subgraph CORE_STATE["Спільний стан (Лише API та Dispatcher)"]
        Redis[("Redis 7<br/>Прогрес, Heartbeat, SSE, Скасування, Логи")]
        PG[("PostgreSQL 16<br/>Користувачі, Задачі, Журнал")]
        Storage["Спільний том /data<br/>uploads, outputs, logs"]
    end

    Dispatcher["Dispatcher Daemon (1 екземпляр)<br/>Вибірка queued, балансування (least_loaded/round_robin),<br/>HTTP-стрімінг файлів, моніторинг, перезапуск збоїв"]

    subgraph WORKER_POOL["Ізольовані Worker-сервери (HTTP :9000)"]
        W1["worker-1 (FastAPI + FFmpeg)<br/>локальний диск, без доступу до БД/Redis"]
        W2["worker-2 (FastAPI + FFmpeg)<br/>локальний диск, без доступу до БД/Redis"]
        WN["worker-remote (Віддалена машина / Autoscaled)<br/>HTTP push :9000"]
    end

    Autoscaler["Autoscaler Daemon<br/>Моніторинг queued задач, Docker SDK"]

    Browser -->|HTTPS :443| Nginx
    Nginx -->|Балансування| API1
    Nginx -->|Балансування| API2
    API_CLUSTER -->|"Створення queued, читання SSE / результатів"| CORE_STATE
    Dispatcher <-->|"Вибірка задач, збереження outputs"| CORE_STATE
    Dispatcher -->|"HTTP POST /execute (стрімінг файлу)"| WORKER_POOL
    WORKER_POOL -->|"HTTP GET progress / result"| Dispatcher
    WORKER_POOL -.->|"Самореєстрація / Heartbeat"| Dispatcher
    Autoscaler -->|"scale up / down контейнерів"| WORKER_POOL
    Autoscaler -.->|"опитування queued задач"| CORE_STATE

    classDef app fill:#EEEDFE,stroke:#534AB7,color:#3C3489
    classDef state fill:#E1F5EE,stroke:#0F6E56,color:#085041
    class Nginx,API1,API2,Dispatcher,W1,W2,WN,Autoscaler app
    class Redis,PG,Storage state
```

---

## 🚀 Запуск однією командою

### 1. Підготовка змінних середовища
```bash
cp .env.example .env
```

### 2. Запуск контейнерів через Docker Compose
```bash
docker compose up --build -d
```

Команда автоматично ініціалізує:
- **`postgres`** — база даних на порту `5432` з healthcheck;
- **`redis`** — сховище швидкого стану (прогрес, прапорці скасування, heartbeat, логи) на порту `6379`;
- **`migrate`** — автоматичне застосування міграцій бази даних через Alembic;
- **`api1`** та **`api2`** — два незалежних бекенд-сервери на базі FastAPI;
- **`dispatcher`** — єдиний екземпляр координатора задач (порт `8100`, вибірка `queued` задач, балансування, стрімінг файлів, моніторинг та перезапуск збоїв);
- **`worker`** — мережевий воркер-сервер на базі FastAPI (порт `9000`, ізольований від БД/Redis/спільного тому `/data`, запуск FFmpeg);
- **`autoscaler`** — служба автоматичного масштабування HTTP-воркерів (2..4 контейнери);
- **`nginx`** — балансувальник навантаження, SSL-термінація та роздача React SPA на портах `80` (HTTP) та `443` (HTTPS).

> [!IMPORTANT]
> **Служба `dispatcher` повинна працювати виключно в одному екземплярі** (не масштабувати репліками через Docker Compose, щоб запобігти конфліктам розподілу задач). Горизонтально масштабуються виключно воркери (`worker`) та сервери API (`api1`, `api2`).

### 3. Доступ до системи
- **Вебінтерфейс (SPA):** [https://localhost](https://localhost)
- **Інтерактивна документація Swagger:** [https://localhost/docs](https://localhost/docs)
- **ReDoc документація:** [https://localhost/redoc](https://localhost/redoc)
- **Облікові дані адміністратора за замовчуванням:**
  - **Email:** `admin@example.com`
  - **Пароль:** `admin_password_123`

---

## 🌐 Запуск воркера на віддаленій машині (Remote Worker)

Завдяки новій HTTP-архітектурі воркер є повністю автономним сервером: йому **не потрібні** спільний том `/data`, доступ до бази даних PostgreSQL чи брокера Redis. Воркер може виконуватися на будь-якому окремому комп'ютері або виділеному сервері:

```bash
# На віддаленому комп'ютері (де встановлено Python 3.10+ та FFmpeg)
cd backend
pip install -r requirements.txt

# Налаштування змінних оточення та запуск HTTP-воркера
export WORKER_ID=remote-worker-1
export WORKER_PORT=9000
export WORKER_CAPACITY=1
export WORKER_PUBLIC_URL=http://<REMOTE_HOST_IP>:9000
export DISPATCHER_URL=http://<DISPATCHER_HOST_IP>:8100
export WORKER_TOKEN=secret_worker_token_change_in_production
export WORKER_DATA_DIR=/tmp/cvp-worker

uvicorn worker_server.main:app --host 0.0.0.0 --port 9000
```

Воркер у фоні автоматично зареєструється у Dispatcher (`POST /internal/workers/register`) і періодично надсилатиме heartbeat (кожні 10 с). Dispatcher автоматично передаватиме йому задачі через HTTP із потоковою передачею відеофайлів, опитуватиме прогрес та забиратиме результат.

---

## ⚙️ Змінні конфігурації `.env`

| Змінна | Тип | За замовчуванням | Опис |
| :--- | :---: | :---: | :--- |
| **`DB_USER`** | `str` | `cvp` | Ім'я користувача PostgreSQL |
| **`DB_PASSWORD`** | `str` | `my_secret_password` | Пароль до бази даних PostgreSQL |
| **`DB_NAME`** | `str` | `cvp` | Назва бази даних |
| **`DB_HOST`** | `str` | `postgres` | Хост сервера бази даних |
| **`DB_PORT`** | `int` | `5432` | Порт сервера бази даних |
| **`REDIS_HOST`** | `str` | `redis` | Хост сервера Redis |
| **`REDIS_PORT`** | `int` | `6379` | Порт сервера Redis |
| **`REDIS_DB`** | `int` | `0` | Номер бази даних Redis |
| **`JWT_SECRET`** | `str` | `super_secure_jwt_...` | Секретний ключ для підпису JWT токенів |
| **`JWT_ALGORITHM`** | `str` | `HS256` | Алгоритм шифрування токенів |
| **`JWT_EXPIRE_MINUTES`** | `int` | `60` | Термін дії сесійного токена у хвилинах |
| **`COOKIE_SECURE`** | `bool` | `true` | Прапорець Secure для cookie токена |
| **`COOKIE_SAMESITE`** | `str` | `lax` | Політика SameSite для сесійних cookie |
| **`WORKER_TOKEN`** | `str` | `secret_worker_token_...` | Секретний токен автентифікації для взаємодії Dispatcher та Worker |
| **`DISPATCHER_URL`** | `str` | `http://dispatcher:8100` | Внутрішня адреса Dispatcher для самореєстрації воркерів |
| **`BALANCING_STRATEGY`** | `str` | `least_loaded` | Стратегія балансування задач між воркерами (`least_loaded` або `round_robin`) |
| **`WORKER_CAPACITY`** | `int` | `1` | Кількість одночасних задач на одного воркера |
| **`DISPATCH_INTERVAL`** | `float` | `1.0` | Інтервал вибірки та розподілу queued задач (секунди) |
| **`WORKER_POLL_INTERVAL`** | `float` | `1.0` | Інтервал опитування прогресу активних задач воркерів (секунди) |
| **`WORKER_FAIL_THRESHOLD`** | `int` | `3` | Кількість невдалих опитувань воркера до позначення як dead |
| **`MAX_TASK_ATTEMPTS`** | `int` | `3` | Максимальна кількість спроб виконання задачі при аварії воркера |
| **`ADMIN_EMAIL`** | `str` | `admin@example.com` | Email системного адміністратора (створюється при старті) |
| **`ADMIN_PASSWORD`** | `str` | `admin_password_123` | Пароль облікового запису адміністратора |
| **`MAX_FILE_MB`** | `int` | `100` | Максимальний розмір відеофайлу у МБ |
| **`MAX_DURATION_SEC`** | `int` | `120` | Максимальна тривалість відеофайлу у секундах |
| **`MAX_ACTIVE_TASKS`** | `int` | `5` | Ліміт активних задач на одного користувача (`queued` + `running`) |
| **`MAX_HISTORY`** | `int` | `20` | Максимальна кількість збережених задач користувача в історії |
| **`MAX_QUEUE`** | `int` | `100` | Максимальна місткість глобальної черги сервера |
| **`RESULT_LIFETIME_HOURS`** | `int` | `24` | Термін зберігання обробленого результату на диску (годин) |
| **`MIN_WORKERS`** | `int` | `2` | Мінімальна кількість воркерів під час масштабування |
| **`MAX_WORKERS`** | `int` | `4` | Максимальна кількість воркерів під час масштабування |
| **`PER_WORKER`** | `int` | `2` | Поріг навантаження (кількість задач у черзі на 1 воркер) |
| **`IDLE_FOR`** | `int` | `60` | Час простою додаткового воркера без задач до видалення (секунд) |

---

## 🛡️ Системні ліміти та поведінка системи

| Ліміт | Значення | Перевірка | Поведінка при перевищенні |
| :--- | :---: | :--- | :--- |
| **Розмір файлу** | 100 МБ | Стрімінговий підрахунок байтів при завантаженні | `422 Unprocessable Content`, переривання з'єднання, видалення часткового файлу |
| **Тривалість відео** | 120 с | Аналіз через `ffprobe` | `422 Unprocessable Content`, видалення завантаженого файлу |
| **Роздільна здатність** | 1920×1080 | Аналіз ширини та висоти відеопотоку `ffprobe` | `422 Unprocessable Content`, видалення завантаженого файлу |
| **Формати відео** | mov, mp4, mkv, webm, avi | Валідація контейнера та наявності відеопотоку | `422 Unprocessable Content`, видалення завантаженого файлу |
| **Активні задачі** | <= 5 на користувача | Підрахунок задач зі статусом `queued` + `running` | `429 Too Many Requests` з чітким повідомленням |
| **Глобальна черга** | <= 100 задач | Підрахунок задач у черзі кластера | `503 Service Unavailable` із заголовком `Retry-After: 30` |
| **Історія задач** | <= 20 на користувача | Перевірка при додаванні нової задачі | Автоматичне видалення найстаріших завершених задач разом із файлами |
| **Дисковий простір** | >= 50 МБ вільного місця | `shutil.disk_usage` перед записом та у воркері | `507 Insufficient Storage` (в API) або статус `failed` (у воркері) |
| **Час життя файлу** | 24 години | Фонове прибирання кожні 10 хв за полем `expires_at` | Автоматичне видалення файлів та очищення запису з бази даних |

---

## 📋 Таблиця «Вимога → Реалізація»

| № | Вимога технічного завдання / КТ | Реалізація у кодовій базі | Файли реалізації |
| :-: | :--- | :--- | :--- |
| **КТ1** | Балансувальник навантаження, HTTPS, `least_conn`, `X-Served-By` | Nginx зворотний проксі, mkcert TLS сертифікати, заголовок `X-Served-By` у middleware FastAPI, 2 сервери `api1` та `api2` | [`nginx/nginx.conf`](nginx/nginx.conf), [`backend/app/main.py`](backend/app/main.py) |
| **п. 1** | Завантаження та валідація відео | Стрімінгове збереження шматками, `probe_video` через ffprobe, валідація тривалості, розміру, потоків, лімітів | [`backend/app/routers/tasks.py`](backend/app/routers/tasks.py), [`backend/app/services/probe.py`](backend/app/services/probe.py) |
| **КТ2** | Координатор Dispatcher та HTTP воркери, FFmpeg | Dispatcher (`least_loaded` / `round_robin`), автономні HTTP воркер-сервери (`/execute`, `/progress`, `/cancel`, `/result`), стрімінг файлів, ізольований диск без БД/Redis, запуск FFmpeg із перехопленням прогресу | [`backend/dispatcher/main.py`](backend/dispatcher/main.py), [`backend/worker_server/main.py`](backend/worker_server/main.py) |
| **п. 2** | Операції обробки відео | Конвертація у MP4 (H.264+AAC), масштабування (1080p..360p з CRF), водяний знак PNG з прозорістю, вилучення аудіо у MP3 / mute | [`backend/worker_server/ffmpeg_runner.py`](backend/worker_server/ffmpeg_runner.py) |
| **п. 3** | Скасування задачі | Прапорець `cancel:{task_id}` у Redis, виклик `/tasks/{id}/cancel` на воркері, зупинка FFmpeg (`proc.terminate()`), очищення незавершеного файлу | [`backend/app/routers/tasks.py`](backend/app/routers/tasks.py), [`backend/worker_server/main.py`](backend/worker_server/main.py) |
| **п. 4** | Відстеження прогресу (SSE) | Ендпоінт `GET /api/tasks/{id}/events` передає поточний статус і відсоток (0..100) з Redis раз на 1 секунду | [`backend/app/routers/tasks.py`](backend/app/routers/tasks.py) |
| **КТ3** | Вебінтерфейс користувача | React 18, TypeScript, адаптивний темний UI, EventSource слухач, перегляд результату через `<video controls>`, керування задачами | [`frontend/src/App.tsx`](frontend/src/App.tsx), [`frontend/src/pages/TasksPage.tsx`](frontend/src/pages/TasksPage.tsx) |
| **п. 6** | Черга та оцінка часу (ETA) | Розрахунок `queue_position` та `estimate_wait_sec` за тривалістю попередніх задач, залишковим прогресом та сумарною місткістю воркерів | [`backend/app/services/estimation.py`](backend/app/services/estimation.py) |
| **п. 7** | Реєстрація воркерів та Autoscaler | Самореєстрація воркерів у Dispatcher (`POST /internal/workers/register`), періодичний heartbeat; служба `autoscaler` масштабує HTTP-воркери за чергою | [`backend/worker_server/main.py`](backend/worker_server/main.py), [`backend/autoscaler/scaler.py`](backend/autoscaler/scaler.py) |
| **КТ4** | Адмін-панель (`/admin`) | `GET /api/admin/overview`, сторінка моніторингу з опитуванням раз на 3 с, картки статусів, таблиця воркерів з CPU/RAM, журнал Dispatcher log, останні 50 задач, журнал масштабування, скасування будь-якої задачі | [`backend/app/routers/admin.py`](backend/app/routers/admin.py), [`frontend/src/pages/AdminPage.tsx`](frontend/src/pages/AdminPage.tsx) |
| **п. 8** | Аудит безпеки та стабілізація | Без `shell=True`, `-protocol_whitelist file`, токен `X-Worker-Token`, файли виключно як UUID, перевірка власника в усіх ендпоінтах, `limit_req`, HttpOnly Secure cookies | [`backend/worker_server/ffmpeg_runner.py`](backend/worker_server/ffmpeg_runner.py), [`backend/app/routers/auth.py`](backend/app/routers/auth.py) |
| **п. 9** | Фонове прибирання | Служба `cleanup_expired_tasks` видаляє застарілі файли та записи з бази раз на 10 хвилин за полем `expires_at` | [`backend/app/services/cleanup.py`](backend/app/services/cleanup.py), [`backend/app/main.py`](backend/app/main.py) |
| **п. 10** | Навантажувальне тестування (Locust) | Сценарій навантаження (реєстрація → вхід → завантаження → опитування), порівняння 2 vs 4 воркери, збереження графіків та CSV | [`locustfile.py`](locustfile.py), [`scripts/benchmark_workers.py`](scripts/benchmark_workers.py), [`docs/evidence/`](docs/evidence/) |

---

## 📡 Опис API (API Reference)

### 1. Автентифікація (`/api/auth`)
- **`POST /api/auth/register`** — реєстрація нового користувача (`email`, `password` від 8 символів). Коди: `201 Created`, `400 Bad Request`.
- **`POST /api/auth/login`** — вхід у систему; встановлює захищену `HttpOnly` cookie `access_token`. Коди: `200 OK`, `401 Unauthorized`.
- **`POST /api/auth/logout`** — вихід із системи та очищення cookie. Коди: `200 OK`.
- **`GET /api/auth/me`** — отримання профілю поточного авторизованого користувача (`id`, `email`, `is_admin`). Коди: `200 OK`, `401 Unauthorized`.

### 2. Керування задачами обробки (`/api/tasks`)
- **`POST /api/tasks`** — завантаження відео (multipart/form-data) та створення задачі:
  - `file`: відеофайл (до 100 МБ, <= 120 с);
  - `operation_type`: `convert` | `scale` | `watermark` | `audio`;
  - `params`: JSON-рядок додаткових параметрів (роздільна здатність, прозорість тощо);
  - `logo`: опційний PNG файл для накладання водяного знака (до 2 МБ).
  - *Коди відповідей:* `201 Created`, `422 Unprocessable Content`, `429 Too Many Requests`, `503 Service Unavailable`, `507 Insufficient Storage`.
- **`GET /api/tasks`** — отримання списку задач поточного користувача. Коди: `200 OK`.
- **`GET /api/tasks/{task_id}`** — деталі задачі з перевіркою власності (статус, прогрес, ETA, черга). Коди: `200 OK`, `403 Forbidden`, `404 Not Found`.
- **`GET /api/tasks/{task_id}/events`** — потік Server-Sent Events (SSE) для відстеження прогресу в реальному часі. Коди: `200 OK` (text/event-stream).
- **`GET /api/tasks/{task_id}/download`** — завантаження готового обробленого медіафайлу (лише для власника або адміна). Коди: `200 OK`, `400 Bad Request`, `404 Not Found`, `410 Gone`.
- **`POST /api/tasks/{task_id}/cancel`** — скасування задачі у черзі або під час обробки. Коди: `200 OK`, `400 Bad Request`, `403 Forbidden`, `404 Not Found`.
- **`DELETE /api/tasks/{task_id}`** — видалення задачі та пов'язаних файлів із диска. Коди: `200 OK`, `404 Not Found`.

### 3. Адміністрування кластера (`/api/admin`, лише для адміністраторів)
- **`GET /api/admin/overview`** — зведені метрики кластера (кількість задач за статусами, довжина черги, активні воркери з CPU/RAM, події масштабування, останні 50 задач). Коди: `200 OK`, `401 Unauthorized`, `403 Forbidden`.
- **`POST /api/admin/tasks/{task_id}/cancel`** — примусове скасування будь-якої активної задачі в кластері адміністратором. Коди: `200 OK`, `403 Forbidden`, `404 Not Found`.
- **`POST /api/admin/cleanup`** — примусовий запуск очищення прострочених задач та файлів з поверненням кількості видалених об'єктів. Коди: `200 OK`.

### 4. Автомасштабування та моніторинг
- **`GET /api/autoscaler/status`** — статус служби автомасштабування та поточна кількість воркерів.
- **`GET /api/autoscaler/events`** — історія подій масштабування (`scale_up`, `scale_down`).
- **`GET /api/health`** — перевірка працездатності екземпляра бекенду із зазначенням `server_id`.

---

## 🧪 Запуск тестів (Pytest)

Проєкт містить вичерпний набір із 86 тестів, що покривають повний життєвий цикл системи:
```bash
# Запуск повного тестового набору
pytest backend/tests

# Запуск із детальним звітом
pytest backend/tests -v
```

Покриття тестів включає:
- `test_auth.py` — реєстрація, логін, захист сесій, валідація email та паролів;
- `test_tasks.py` — завантаження файлів, валідація `ffprobe`, перевірка лімітів;
- `test_worker.py` — обробка задач через `process_task`, аргументи FFmpeg, перевірка скасування;
- `test_worker_server.py` — робота автономного HTTP воркер-сервера (`/execute`, `/progress`, `/cancel`, `/result`, перевірка токена, лімітів місткості та диску);
- `test_dispatcher.py` — балансувальник задач, стратегії `least_loaded` / `round_robin`, реєстр воркерів, стрімінг задач, моніторинг, обробка аварій воркерів та перепризначення;
- `test_day6.py` — Server-Sent Events (SSE), завантаження результатів, водяні знаки;
- `test_day9.py` — черга, розрахунок позиції та ETA, Retry-After;
- `test_day10.py` — heartbeat воркерів, autoscaler алгоритм, моніторинг ресурсів;
- `test_day11.py` — адмін-ендпоінти, перевірка доступу 401/403, огляд кластера;
- `test_day12.py` — аудит безпеки, protocol whitelist, санітизація UUID, очищення застарілих задач;
- `test_day13.py` — ліміти активних задач та історії, парсинг виводу FFmpeg на тестових рядках, прапорець скасування;
- `test_e2e_flow.py` — повні наскрізні сценарії обробки чотирьох типів операцій, скасування та ізоляції.

---
