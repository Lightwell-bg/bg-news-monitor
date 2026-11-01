# BG News Monitor

An MVP service that selects important Bulgarian news for a Russian-speaking audience. It finds new articles, removes duplicates, prepares a Russian draft and sends the candidate to an administrator in Telegram.

Publishing to the channel is only possible after the «✅ Одобрить» (Approve, with a random delay) or «⚡ Сразу» (Publish now) button. The AI never publishes anything and never replaces the permission and status checks done in code.

Russian version: [README_RU.md](README_RU.md). Quick start by steps (in Russian): [docs/QUICK_START.md](docs/QUICK_START.md).

## What the service does

1. The scheduler polls the enabled sources at each source's interval.
2. An adapter parses the listing page and finds article links.
3. The link is normalized; a repeat by normalized URL or by content hash gets the `DUPLICATE` status.
4. Deterministic rules drop service sections, too-short and too-old items **before** the AI is called.
5. OpenRouter returns an assessment and a Russian draft; the answer is validated with a Pydantic schema.
6. Code guards reject a draft that copies the article verbatim or contains numbers absent from the source.
7. The candidate goes to the administrators as a private card. After «Approve» the news is scheduled (`SCHEDULED`, random 5–N minutes); after «Publish now», or when the time comes, it becomes `APPROVED`, and only then does the code post it to the channel.

Supported sources (homepage adapters):

| Source | Site | State in `config/sources.yaml` |
| --- | --- | --- |
| **Flagman** | https://www.flagman.bg/ | enabled |
| **BG24** | https://bg-24.com/ | disabled, can be enabled from Telegram |
| **Burgas24** | https://www.burgas24.bg/ | enabled, polled every 30 minutes |

The Burgas24 adapter takes only articles of the form `/novini/<rubric>/<title>-<number>` on the `burgas24.bg` domain from the homepage; classifieds, videos, topics, per-day archives, sport, the directory and the network's partner sites are skipped. A page without a title or article text is not processed.

Administrators manage sources straight from Telegram: enable and disable, polling interval, sections and inclusion/exclusion topics. Changes are validated with a Pydantic schema, saved by atomically replacing `config/sources.yaml` and applied by the running process without duplicating scheduler jobs.

## Stack

- Python 3.12, aiogram 3, httpx, selectolax, pydantic-settings, PyYAML;
- SQLAlchemy 2, aiosqlite, SQLite, APScheduler;
- OpenRouter HTTP API and pytest.

## Structure

```text
src/news_monitor/
  app.py              # composition root and entry point
  pipeline.py         # flow: listing -> article -> dedup -> filter -> AI -> card
  scheduler.py        # source intervals (APScheduler)
  reports.py          # daily moderation report
  logging_config.py   # logging with mandatory secret redaction
  config/             # Settings, reading and safely editing sources.yaml
  sources/            # contracts, Flagman, BG24 and Burgas24 adapters, HTTP fetching
  normalization/      # URL normalization and content hash
  filters/            # deterministic rules and draft code guards
  ai/                 # prompt, strict Pydantic schema, OpenRouter client
  storage/            # models, statuses, atomic transitions
  telegram/           # card, roles, callbacks, publishing, admin panel
config/sources.yaml   # source definitions
config/runtime.yaml   # settings changed by an administrator (created automatically)
tests/                # tests and local HTML fixtures
```

## Running

Virtual environment (Windows):

```bash
python -m venv .venv
source .venv/Scripts/activate
```

Install dependencies:

```bash
pip install -e ".[dev]"
```

Run the service:

```bash
python -m news_monitor
```

Tests:

```bash
pytest
```

Tests do not use the network: an automatic fixture blocks outgoing connections, HTML comes from `tests/fixtures/`, Telegram and the AI are replaced with fakes.

## Connecting third-party services

Copy `.env.example` to `.env` and fill in the values. `.env` is not committed; `.env.example` holds empty examples only.

| Variable | Purpose | Where to get it |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token of the bot that sends cards and posts | [@BotFather](https://t.me/BotFather) → `/newbot`; see the [docs](https://core.telegram.org/bots/features#botfather) |
| `TELEGRAM_ADMIN_IDS` | Numeric administrator IDs, comma-separated; only they control the buttons | [@userinfobot](https://t.me/userinfobot) or `getUpdates` |
| `TELEGRAM_ADMIN_ID` | A single administrator, backward compatibility | Same |
| `TELEGRAM_CHANNEL_ID` | Publication channel: `@username` or numeric `-100...` | Add the bot as a channel administrator |
| `OPENROUTER_API_KEY` | OpenRouter key | [openrouter.ai/keys](https://openrouter.ai/keys); see the [docs](https://openrouter.ai/docs) |
| `OPENROUTER_MODEL` | Model identifier | [openrouter.ai/models](https://openrouter.ai/models) |
| `CHECK_INTERVAL_MINUTES` | Minimum polling interval | — |
| `IMPORTANCE_THRESHOLD` | Importance threshold 0–100 for a card; the default value, later changed from Telegram | — |
| `PUBLISH_DELAY_MAX_MINUTES` | Maximum publication delay after «Approve», 5–1440 minutes (default 60); later changed from Telegram | — |
| `DAILY_REPORT_TIME` | Time of the daily moderation report, `HH:MM` Sofia time (default `09:00`); recipients are chosen in the bot | — |
| `DATABASE_URL` | SQLAlchemy URL, local SQLite by default | — |
| `DRY_RUN` | `true` — the card is sent, nothing goes to the channel | — |

### Telegram setup

1. Create a bot with `@BotFather` and save the token in `.env`.
2. Write to the bot in a private chat, otherwise it cannot send you a card.
3. Add the bot as a channel administrator with the right to post messages.
4. Set `TELEGRAM_CHANNEL_ID`. With `DRY_RUN=true` the channel is optional.

### Additional optional variables

They have sensible defaults and are rarely needed: `OPENROUTER_BASE_URL`, `OPENROUTER_TIMEOUT_SECONDS`, `SOURCES_FILE`, `MAX_ARTICLES_PER_RUN`, `REQUEST_TIMEOUT_SECONDS`, `USER_AGENT`.

## Telegram admin panel

The panel is available only to numeric IDs from `TELEGRAM_ADMIN_IDS` (or `TELEGRAM_ADMIN_ID`). Permissions are checked twice: by the router filter and by the service.

| Command | Action |
| --- | --- |
| `/sources` | Source list: one button per source |
| `/settings` | Settings screen with the importance threshold |
| `/moderators` | Moderators and daily report recipients |
| `/check` | Check all enabled sources now (also the «🔄 Проверить новости сейчас» button in `/settings`; at most once per 2 minutes) |

Everything else is done with buttons; no technical identifiers need to be typed:

- list → source button → card with state, interval, sections and topics;
- in the card: enable/disable, interval ±5 minutes or «✏️ Интервал», sections, inclusion and exclusion topics;
- «➕ Добавить раздел/тему» and «✏️ Интервал» start a short dialog: the panel waits for one message and shows a «✖️ Отмена» button. A section is sent as `Name | https://…`;
- an unfinished question is dropped by «Отмена», by any other panel button, or by timeout, so a stray message is never taken for an answer;
- a wrong answer saves nothing: the panel shows the reason and repeats the question.

### Importance threshold

The «⚙️ Настройки» screen shows the current `IMPORTANCE_THRESHOLD` and changes it with ±5/±10 buttons or by typing a value. The value is validated against 0–100, applied to the running process at once and saved to `config/runtime.yaml` next to `sources.yaml`.

### Publication delay

A card has three actions:

- «✅ Одобрить (5–N мин)» schedules publication at a random time between 5 and N minutes (N is the maximum delay, 60 by default). The card gets a line «⏰ Публикация в HH:MM (София)» and the buttons «⚡ Опубликовать сейчас» and «↩️ Отменить»;
- «⚡ Сразу» publishes without delay;
- «↩️ Отменить» on a scheduled card returns the news to awaiting a decision with all the original buttons.

N is changed in «⚙️ Настройки»: ➖/➕ buttons (±10, ±30 minutes) or «✏️ Ввести задержку». Allowed range is 5 to 1440 minutes; the value is applied at once and saved to `config/runtime.yaml`. The default comes from `PUBLISH_DELAY_MAX_MINUTES` (60 by default, range 5–1440).

The schedule is stored in the database, so it survives a restart: the scheduler checks the queue about every 30 seconds, and items that became overdue during downtime are published at the first check after startup. After a scheduled publication the administrator card is updated (buttons removed, status added). Time is shown in Sofia time (Europe/Sofia); the database stores UTC.

`config/runtime.yaml` holds non-secret values only, is created on the first change and is not committed. At startup a saved value overrides `IMPORTANCE_THRESHOLD` from the environment; delete the file to return to the `.env` value. The panel never reads or changes `.env`.

How source saving works:

- a change is first validated as a whole by the Pydantic schema; on error the file stays untouched and the administrator gets a clear message;
- the write goes to a temporary file and finishes with an atomic replacement of `SOURCES_FILE` only; comments at the top of the file are kept;
- after saving, the process re-reads the file and re-registers scheduler jobs, one job per source, so an interval change creates no duplicates;
- if a change cannot be applied at runtime (for example a source has no registered adapter), the setting is still saved and the answer says explicitly that a restart is needed;
- `.env` and tokens are neither edited nor shown here;
- button callback data fits the Telegram limit of 64 bytes, and long screens are cut by lines.

## Moderators and daily report

Administrators (`TELEGRAM_ADMIN_IDS`) can assign moderators right in the bot. A moderator receives the same news cards but sees only the «Одобрить» (Approve) and «Отклонить» (Reject) buttons: «Сразу», «Отменить», regeneration, sources, settings and this section are not available to them. Whoever presses first decides; the other card recipients see who decided.

How to assign:

1. Send the bot `/moderators` (or press «👥 Модераторы» in `/settings`).
2. Press «➕ Добавить» and pick the person in one of three ways: the «👤 Выбрать пользователя» button at the bottom of the screen, a numeric Telegram ID sent as a message, or a message forwarded from that person. If the person hides their account, ask them to send `/start` to the bot: it shows their ID.
3. The new moderator must open the bot once and press «Запустить» (Start), otherwise Telegram does not let the bot write to them first. Any stranger who sends `/start` only sees their own ID.

To remove: `/moderators` → «🗑 Name» → «Да, удалить». At most 50 moderators. An administrator cannot be a moderator: they already have every right.

Daily report: every day at `DAILY_REPORT_TIME` (format `HH:MM`, default `09:00`, Europe/Sofia time) the chosen administrators receive a report for the previous day: how many items were approved, rejected, cancelled and who made the decisions. Recipients are chosen in the bot: `/moderators` → «📊 Отчёт» (by default only the owner, the first ID in `TELEGRAM_ADMIN_IDS`, receives it; the list is stored in `config/runtime.yaml`). The «📨 Прислать сводку за сегодня» button sends the report to the person who pressed it only. Moderators are stored in the database and survive a restart.

## Deploying to a VPS with Docker

The service opens no HTTP ports: it works through Telegram long polling. On a VPS where Docker and Docker Compose are already installed, clone the public repository as the user who will maintain the project (`vlad` in the examples):

```bash
sudo mkdir -p /opt/bg-news-monitor
sudo chown vlad:vlad /opt/bg-news-monitor
git clone https://github.com/Lightwell-bg/bg-news-monitor.git /opt/bg-news-monitor
cd /opt/bg-news-monitor
```

Create the secret configuration file and fill in the required values from the table above. Keep `DRY_RUN=true` for the first start:

```bash
cp .env.example .env
nano .env
sudo docker compose up -d --build
sudo docker compose ps
sudo docker compose logs -f --tail=100 news-monitor
```

Stopping the log view with `Ctrl+C` does not stop the container. Stop the service itself with:

```bash
sudo docker compose down
```

Do not delete the `data/` directory: it holds SQLite (including moderators), the sources saved through Telegram and the runtime settings. They survive rebuilding and updating the container.

### Updating on the VPS

After pushing a new version to GitHub:

```bash
cd /opt/bg-news-monitor
git pull --ff-only
sudo docker compose up -d --build
sudo docker compose logs --tail=100 news-monitor
```

## Security and quality

- Control buttons and source management commands are available only to numeric IDs from `TELEGRAM_ADMIN_IDS`; permissions are checked by both the router filter and the service. Moderators can only approve and reject.
- Pressing a button twice does not create a second post: the status transition is a conditional UPDATE that exactly one call wins.
- Uniqueness is guaranteed by the normalized URL and the content hash.
- An invalid AI answer creates neither a card nor a post.
- A draft with a verbatim copy of the article or with numbers absent from the source is rejected by code.
- A link to the original material is mandatory in the post.
- Each post is followed by one quote block with project links, 4 rows of 2–3 links separated by « · » (the `CHANNEL_FOOTER_ROWS` list in `src/news_monitor/telegram/card.py`). The addresses are shown only as text links. The block comes after the source link, so the Telegram preview is built from the article.
- Tokens and keys come only from the environment, are masked in logs and never appear in reports.
