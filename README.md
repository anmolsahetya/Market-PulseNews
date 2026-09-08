# Pulse → Telegram

Scrapes [pulse.zerodha.com](https://pulse.zerodha.com/) every 5 minutes and posts each new
article to a Telegram channel as its own message.

---

## ⚠️ Make the repository public

At a 5-minute schedule this runs ~288 times a day. GitHub bills each job rounded up to a
whole minute, so that is roughly **8,600 Actions minutes a month**.

| Repo visibility | Free Actions minutes / month | Verdict |
|---|---|---|
| **Public** | Unlimited | ✅ Use this |
| Private | 2,000 | ❌ Exhausted in ~7 days, then billed |

No secrets live in the code — the bot token is stored in GitHub **Secrets**, which stay
private even on a public repo. If the repo must be private, raise the cron interval to
`*/30` (~1,440 min/month, still under the cap) or run it on a VPS instead.

---

## Setup

### 1. Create the bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram and send `/newbot`.
2. Follow the prompts; it replies with a token like `1234567890:AAE...`.
3. Keep that token secret — anyone holding it controls the bot.

### 2. Add the bot to your channel

1. Open your channel → **Manage Channel** → **Administrators** → **Add Administrator**.
2. Add your bot and grant it **Post Messages**. This permission is required; without it
   every send fails with a 403.

### 3. Find the chat ID

- **Public channel:** the chat ID is just `@yourchannelname`.
- **Private channel:** post any message in the channel, then open
  `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read
  `result[].channel_post.chat.id`. It looks like `-1001234567890` (keep the minus sign).

### 4. Add the repository secrets

In the repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | `@yourchannel` or `-100…` |

Add these through the GitHub web UI yourself — never commit them to a file.

### 5. Enable and start

Open the **Actions** tab and enable workflows if prompted. Then run
**Pulse to Telegram → Run workflow** manually once.

**The first run posts nothing.** It records the ~300 articles currently on the page as
"already seen", so your channel doesn't receive a day of backlog at once. Every run after
that posts only genuinely new articles.

To preview without posting, run the workflow manually with **dry_run** ticked and read the
job log.

---

## How it works

`pulse.zerodha.com` is fully server-rendered: one `GET` returns ~300 articles covering the
last ~24 hours. No browser or JavaScript is needed.

```
ul#news > li.box.item[id="item-<numeric id>"]
  ├── h2.title > a[href]          headline + destination link
  ├── div.desc                    one-line summary
  ├── span.date[title]            "08:57 PM, 08 Sep 2026" (IST)
  └── span.feed                   "— Economic Times"
```

### Deduplication uses a set, not a watermark

Pulse sorts its homepage by **publish time**, but assigns item ids at **ingestion time**,
and those two orders do not agree — on a live sample of 307 items the ids were *not*
monotonically decreasing down the page. An article ingested late carries a high id but an
older timestamp, so it sorts below articles with lower ids.

Tracking only "the highest id seen so far" would therefore skip those articles **silently
and permanently**. The bot keeps an explicit set of posted ids instead, in `seen.json`.

The set is bounded to the 5,000 highest ids (~2 weeks at Pulse's ~300 articles/day). The
homepage only reaches back 24 hours, so anything older can never reappear.

### State persistence

`seen.json` lives on a dedicated orphan branch called `state`. Each run force-pushes a
fresh single-commit history, so the branch never grows despite being written ~288 times a
day, and your main branch history stays clean.

### Reliability

- **Delayed runs are harmless.** GitHub frequently delays scheduled workflows under load
  and occasionally skips one. Because the page holds 24 hours and dedup is by id, a late
  run just catches up — nothing is lost. Delay affects latency only.
- **Overlapping runs can't double-post.** A `concurrency` group serializes them.
- **Partial failures don't repeat.** State is saved even if the job fails midway, so
  already-sent articles are never sent twice.
- **A corrupt state file fails closed** — it re-seeds rather than re-posting the backlog.
- **Rate limits are honoured.** Telegram allows ~20 messages/minute to one chat; the
  publisher paces sends at ~17/minute and obeys `retry_after` on HTTP 429. At Pulse's real
  volume (~1–2 new articles per 5-minute run) this rarely engages.
- **`MAX_PER_RUN=40`** is a flood guard: if something upstream goes wrong and the diff
  explodes, the channel gets the 40 newest and the rest are marked seen.

### Message format

> **[Headline as a link]**
>
> One-line description.
>
> *Economic Times · 08 Sep 2026, 08:57 PM IST*

All scraped text is HTML-escaped — headlines routinely contain `&`, `%`, `₹` and
apostrophes, any of which would otherwise make Telegram reject the message.

---

## Configuration

All settings come from environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(required)* | BotFather token |
| `TELEGRAM_CHAT_ID` | *(required)* | `@channel` or `-100…` |
| `DRY_RUN` | `0` | `1` prints messages instead of sending |
| `SEED_ON_FIRST_RUN` | `true` | First run records the backlog silently |
| `MAX_PER_RUN` | `40` | Flood guard |
| `STATE_PATH` | `state/seen.json` | Where the seen-id set is stored |

Changing the interval: edit the `cron` line in `.github/workflows/pulse.yml`. GitHub's
minimum is 5 minutes; anything shorter is silently rounded up.

---

## Running locally

```bash
pip install -r requirements.txt

# Preview without posting - no token needed
DRY_RUN=1 python -m pulsebot.main

# Post for real
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_CHAT_ID='@yourchannel'
python -m pulsebot.main
```

Tests run against markup copied verbatim from the live page:

```bash
python -m unittest discover -s tests -v
```

---

## Maintenance

- **GitHub disables scheduled workflows after 60 days of repository inactivity** and
  emails you first. Any push re-enables them, or click *Enable workflow* in the Actions tab.
- **If Pulse changes its markup**, the scraper raises
  `Could not find <ul id='news'>` or `extracted zero articles` and the job fails loudly
  rather than silently posting nothing. Update the selectors in `pulsebot/scraper.py`.
- **To reset**, run the workflow manually with **reset_state** ticked — it re-seeds from
  the current page and posts nothing.

## Courtesy

One request every 5 minutes is ~288 hits/day to a public page — modest, but Pulse is a
free service Zerodha runs for the community. Don't lower the interval further, and if you
share the channel publicly, credit Pulse as the source.

## Layout

```
pulsebot/
  scraper.py    fetch + parse Pulse HTML into Article objects
  telegram.py   render + send via the Bot API, with retries and throttling
  state.py      bounded seen-id set, persisted as JSON
  config.py     environment-variable configuration
  main.py       entry point: scrape → diff → post → persist
tests/
  fixture.html  markup copied verbatim from the live page
  test_pulsebot.py
.github/workflows/
  pulse.yml     the 5-minute scheduled relay
  test.yml      unit tests on push and PR
```
