# Розгортання на Linux / VPS

Інструкція для одного процесу бота на сервері з Docker і Compose. Образ перевірено
на Linux ARM64. Збирання, залежності, імпорт, офлайн-eval і збереження каталогу
в volume перевірено; реальний Telegram ще потребує тестового токена й ручного
тесту. Адреси сервера, SSH-реквізити й секрети до репозиторію не додаються.

## Підготовка й запуск

1. Через власний SSH-профіль під'єднайтеся до сервера. Перевірте `docker version`
   та `docker compose version`. Бот не відкриває вхідних портів: потрібен вихідний
   HTTPS до Telegram і, лише при ввімкненні LLM, Anthropic.
2. Розмістіть вихідний проєкт, наприклад у `~/apps/shop-assistant-bot`.
   Не передавайте локальні `.env`, `.venv`, `data`, `.git` або клієнтські дані.
3. У каталозі проєкту створіть серверний `.env` з правами лише власника:

   ```sh
   cd ~/apps/shop-assistant-bot
   umask 077
   cp -n .env.example .env
   chmod 600 .env
   ```

   Редактором на сервері задайте `TELEGRAM_BOT_TOKEN`, `MANAGER_TELEGRAM_ID`,
   `LLM_ENABLED=false`; для першого порожнього каталогу явно задайте
   `DEMO_CATALOG_PATH=/app/examples/catalog.csv`. Наявні товари не перезаписуються.
   Не виводьте секрети в логи або чат.
4. Запустіть і перевірте стан:

   ```sh
   docker compose --env-file .env up -d --build
   docker compose ps
   docker compose logs --tail 50 bot
   docker compose exec -T bot python -m pip check
   docker compose exec -T bot python evals/run.py
   docker compose exec -T bot python -m shop_assistant --db /data/catalog.sqlite3 search --sku 00123
   ```

   `ps` підтверджує стан контейнера, але не успішний діалог у Telegram.
   Менеджер має відкрити чат із ботом і натиснути Start. Виконайте
   [ручний сценарій](DEMO.md), перш ніж вважати Telegram перевіреним.
5. Виконайте `docker compose restart bot` і повторіть пошук: каталог, звернення,
   черга та бюджет мають зберегтися. У Compose зафіксовано ім'я проєкту
   `shop-assistant-bot`: каталоги різних релізів використовують той самий volume.
   Не перевизначайте його через `-p` або `COMPOSE_PROJECT_NAME` і не запускайте
   одночасно різні релізи. Команди Compose виконуйте з каталогу поточного релізу.

Compose підставляє `.env`; Python сам його не читає. Токени в оточенні контейнера
доступні адміністраторам Docker. Не запускайте `docker compose config`,
`docker inspect` чи `env` у публічному лозі — вони можуть розкрити секрети.
Runtime-образ працює під UID 10001, містить приклад каталогу та eval-runner,
але не тести й README.

`restart: unless-stopped` повертає сервіс після аварії або перезапуску Docker.
Запускайте одну polling-репліку токена; паралельний локальний запуск зупиніть.
Compose не налаштовує зовнішній моніторинг, дискові квоти чи віддалені копії.

## Узгоджена резервна копія SQLite

Volume `catalog_data` містить каталог, закриті поля, контекст звернень,
вхідні updates, outbox і витрати LLM. Просте копіювання відкритого SQLite-файлу
може пропустити транзакції WAL. SQLite backup API створює узгоджений знімок
навіть при запущеному боті.

На сервері з каталогу проєкту виконайте команду нижче. Вона створює нову копію
з UTC-часом у назві й друкує тільки шлях:

```sh
docker compose exec -T bot python -c 'import sqlite3; from pathlib import Path; from datetime import datetime, timezone; folder=Path("/data/backups"); folder.mkdir(exist_ok=True); path=folder/("catalog-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+".sqlite3"); source=sqlite3.connect("file:/data/catalog.sqlite3?mode=ro", uri=True); target=sqlite3.connect(path); source.backup(target); target.close(); source.close(); print(path)'
```

Копія в тому самому volume захищає від невдалого імпорту, але не від втрати
volume або сервера. Скопіюйте її за межі volume; `BACKUP_NAME.sqlite3` замініть
на надруковану назву:

```sh
umask 077
mkdir -p backups
docker compose cp bot:/data/backups/BACKUP_NAME.sqlite3 ./backups/BACKUP_NAME.sqlite3
chmod 600 ./backups/BACKUP_NAME.sqlite3
```

За власною процедурою перенесіть копію на інший захищений носій. Автоматичного
розкладу й зовнішнього сховища проєкт не налаштовує. Не додавайте БД до Git
або відео. Періодично перевіряйте відновлення на окремому тестовому volume.

## Відновлення

Відновлення **замінює всі дані поточної БД**: каталог, діалоги, чергу, updates
і бюджет. Спочатку зробіть свіжу копію поточного стану за процедурою вище.
Перевірте дату й назву копії та зупиніть усі процеси, що працюють із цією БД.

Покладіть обрану копію в `./backups/SELECTED.sqlite3`, замініть назву в команді,
якщо вона інша. Копія на хості має права `600` й може належати іншому UID,
тому лише одноразова адміністративна команда відновлення працює як root.
Вона перевіряє знімок, відновлює його через backup API, а потім повертає БД
власнику UID/GID 10001 і встановлює права `600`. Звичайний бот залишається без root:

```sh
docker compose stop bot
docker compose run --rm --no-deps --user 0:0 -v "$PWD/backups:/restore:ro" bot python -c 'import os, sqlite3; source=sqlite3.connect("file:/restore/SELECTED.sqlite3?mode=ro", uri=True); check=source.execute("PRAGMA quick_check").fetchone()[0]; assert check == "ok", "Backup integrity check failed"; target=sqlite3.connect("/data/catalog.sqlite3"); source.backup(target); target.close(); source.close(); os.chown("/data/catalog.sqlite3", 10001, 10001); os.chmod("/data/catalog.sqlite3", 0o600); print("Database restored")'
```

Якщо команда відновлення завершилася успішно, запустіть і перевірте бота:

```sh
docker compose up -d bot
docker compose exec -T bot python -m shop_assistant --db /data/catalog.sqlite3 search --sku 00123
```

При помилці не запускайте сервіс до з'ясування стану БД. Перевірте активні
звернення через `/tickets`. Знімок відображає момент копіювання: повідомлення,
доставлені пізніше, можуть знову потрапити в outbox; витрати після знімка не
враховані. Звірте бюджет із провайдером перед увімкненням платної LLM.

## Оновлення та зупинка

Перед оновленням перевірте копію, зупиніть бота, оновіть вихідні файли й повторіть
`docker compose up -d --build`. Каталог автоматично не замінюється; невідома
версія схеми зупиняє запуск. Старіша версія коду може не підтримувати новішу
схему — перевіряйте відкат на копії.

`docker compose stop bot` або `docker compose down` зберігає named volume.
**Не використовуйте `docker compose down -v` для звичайної зупинки:** це видалить
весь стан. Застосунок очікує активні операції до 80 секунд, Compose — до 90;
невідправлені повідомлення залишаються для наступного запуску.

Платний режим і публічне демо не випливають із самого розгортання. Перед
ввімкненням потрібні погоджені витрати, актуальні тарифи, ненульовий бюджет
та [реальна оцінка моделі](../evals/README.md).
