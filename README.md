# Pulse → Telegram

Scrapes [pulse.zerodha.com](https://pulse.zerodha.com/) every 5 minutes and posts each new
article as its own message to **one or more** Telegram channels.

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
| `TELEGRAM_CHAT_IDS` | `@chan1,@chan2,-1001234567890` — one or many |

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

## Testing before you go live

Work through these in order. Each stage adds one new failure mode, so if
something breaks you know exactly what caused it.

### Stage 1 — Scraper only, no Telegram, no secrets

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v   # 19 tests, offline
DRY_RUN=1 python -m pulsebot.main         # hits Pulse, prints, sends nothing
```

The dry run prints the newest few rendered messages and proves Pulse is
reachable and the markup still parses. It never sends anything and never
writes state, so it is safe to run repeatedly — including on a cold start,
where it previews rather than seeding.

### Stage 2 — Preflight the whole chain

```bash
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_CHAT_IDS='@chan1,@chan2'
python -m pulsebot.verify
```

Checks, in order: Pulse is reachable and parses; the token authenticates;
the channel exists; **the bot is an admin that may actually post**. That last
one is the most common setup mistake and the API only tells you at send time
otherwise.

Add `--send` to post one real test message to the channel:

```bash
python -m pulsebot.verify --send
```

### Stage 3 — A throwaway channel first

Point `TELEGRAM_CHAT_IDS` at a private test channel and run the real thing:

```bash
python -m pulsebot.main   # first run: seeds, posts nothing
python -m pulsebot.main   # second run onward: posts new articles
```

Wait ~10 minutes between the second and third run and confirm you get a
small number of genuinely new articles, with no repeats. Repeats mean state
isn't persisting; check `state/seen.json` is being written.

### Stage 4 — GitHub Actions, still on the test channel

Push, add the secrets, then **Actions → Pulse to Telegram → Run workflow**:

1. Run once with **dry_run** ticked. Read the log — nothing is sent.
2. Run again normally. This seeds; a `state` branch appears.
3. Run a third time. Real posts land in the test channel.

Confirm the `state` branch exists and holds `seen.json` with a few hundred
ids. If it doesn't, the save step failed — check the log for a push error.

### Stage 5 — Go live

Swap `TELEGRAM_CHAT_IDS` to the real channel(s), delete the `state` branch so it
re-seeds cleanly against the new channel, and let the schedule take over.

> Deleting the `state` branch matters: state carried over from the test
> channel would suppress articles the real channel has never seen.

---

## Running in production

### Knowing when it breaks

GitHub emails you when a scheduled workflow fails, but **only for hard
failures**. Two silent modes are worth knowing:

- **Pulse changed its markup.** The scraper deliberately raises rather than
  posting nothing, so this surfaces as a failed run and an email. Working as
  intended.
- **The schedule stopped.** GitHub disables scheduled workflows after
  **60 days of repository inactivity** (it emails first), and silently skips
  runs under heavy load. If the channel goes quiet, check the Actions tab
  before suspecting the code.

A simple health check: if nothing has posted in ~3 hours during Indian market
hours, something is wrong — Pulse publishes ~13 articles/hour.

### Cost

Public repo: free. Private repo: see the warning at the top of this file.

### Tuning

If the channel feels too noisy once it's live, the cheapest fix is filtering
by source in `pulsebot/main.py` — Pulse only carries 5 (The Hindu Business,
Economic Times, NDTV Business, Business Standard, Finshots), so dropping one
or two meaningfully cuts volume.

---

## Multiple channels

Set `TELEGRAM_CHAT_IDS` to a comma-separated list. Every channel receives the
same articles.

```
TELEGRAM_CHAT_IDS=@Market_PulseNews,@SecondChannel,-1001234567890
```

Adding or removing a channel is a secret edit — no code change, no deploy.
Commas, newlines and spaces all work as separators, blanks are ignored, and a
channel listed twice is de-duplicated (otherwise it would receive everything
twice). `TELEGRAM_CHAT_ID` singular is still honoured, so an existing
single-channel setup keeps working untouched.

**The bot must be an administrator with "Post Messages" in every channel.**
`python -m pulsebot.verify` checks each one individually and names any that fail.

### State is per channel

`seen.json` keeps a separate set of posted article ids for each channel:

```json
{
  "version": 2,
  "channels": {
    "@Market_PulseNews": { "seen_ids": [...], "count": 306 },
    "@SecondChannel":    { "seen_ids": [...], "count": 306 }
  }
}
```

This is not incidental. A single shared set would break in three ways:

- A channel **added later** would be instantly "caught up" and never receive anything.
- If a send **succeeds for channel A and fails for channel B**, marking the article
  seen globally means **B loses it permanently** — silent, invisible data loss.
- Channels fail independently in practice: the bot gets removed from one, loses
  its post permission in another, or one chat hits a rate limit.

Per-channel state means each channel advances only on its own successful sends,
so a channel that was broken for an hour receives everything it missed once fixed.

An older single-channel `seen.json` is migrated automatically: its ids become the
starting baseline for every configured channel.

### A newly added channel seeds, it does not backfill

The first run that sees a new channel records the ~300 articles currently on the
page as "seen" and posts nothing to it. From the next run it receives new
articles like every other channel. This stops a new channel getting a day's
backlog dumped into it at once. Set `SEED_ON_FIRST_RUN=false` if you'd rather it
did backfill.

### One broken channel does not stop the rest

A channel that can't be posted to (wrong id, bot not an admin, permission
revoked) is logged, skipped, and the run continues to the other channels. The
run then exits non-zero so GitHub marks it failed and emails you — a channel
silently going dark is worse than a red build. A bad **bot token**, by contrast,
affects everything, so it aborts immediately.

### Volume and rate limits

Messages per run = new articles × channels. Telegram's ~20 messages/minute limit
is **per chat**, so the publisher tracks a separate clock for each channel —
adding channels doesn't slow down the ones you already have. A separate small
floor between any two sends keeps the bot under its global ~30/second cap.

At Pulse's ~1–2 new articles per 5-minute run, ten channels is about 20 messages
per run, comfortably within limits.

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
| `TELEGRAM_CHAT_IDS` | *(required)* | Comma-separated channels; all receive the same articles |
| `DRY_RUN` | `0` | `1` previews the newest few messages; sends nothing, writes no state |
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
export TELEGRAM_CHAT_IDS='@chan1,@chan2'
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
  state.py      bounded per-channel seen-id sets, persisted as JSON
  config.py     environment-variable configuration
  main.py       entry point: scrape → per-channel diff → post → persist
  verify.py     preflight: checks Pulse, the token, and channel permissions
tests/
  fixture.html  markup copied verbatim from the live page
  test_pulsebot.py
.github/workflows/
  pulse.yml     the 5-minute scheduled relay
  test.yml      unit tests on push and PR
```
