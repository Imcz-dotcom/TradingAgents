# Execution Agent

TradingAgents researches a stock and prints a decision, but it never *does*
anything with it. This agent closes that gap: it runs the research, then **asks
you on your phone** how much to trade and whether to go ahead — and only if you
tap **Approve** does it place the order.

```mermaid
flowchart TD
    Start([You: send a ticker, or run czagent]) --> Research[Run TradingAgents research]
    Research --> Rating{Rating?}
    Rating -->|Buy or Sell signal| Ask[Telegram: How much?]
    Rating -->|Hold or REVIEW| Skip[No order — done]
    Ask --> Amount[You type a number]
    Amount --> Unit{Unit?}
    Unit -->|bare number| AskUnit[Telegram: Dollars or Shares?]
    Unit -->|shares| Qty[Quantity = shares]
    Unit -->|dollars| Price[Fetch live price]
    AskUnit --> Qty
    AskUnit --> Price
    Price --> Calc[Quantity = amount / price]
    Qty --> Approve[Telegram: Approve or Reject?]
    Calc --> Approve
    Approve -->|Approve| Place[Place order on Moomoo — paper by default]
    Approve -->|Reject or timeout| Done[No order]
    Place --> Audit[Write to audit log]
```

The safety model is simple: **nothing reaches the broker unless you tap
Approve.** Timeout = reject, never a silent "yes."

## What you need

1. **Moomoo + OpenD** — the free gateway that lets your computer talk to Moomoo.
2. **A Telegram bot** — so you get the Approve/Reject message on your phone.
3. The two Python extras installed (see below).

## 1. Install

From the repo root, first activate the project's virtual environment, then install:

```bash
source .venv/bin/activate
pip install -e ".[execution]"
```

The `source` line puts you inside the project's Python environment — after it,
your prompt should start with `(.venv)`. If it doesn't, `czagent` won't be found.
Re-run that `source` line each time you open a fresh terminal.

This pulls in `moomoo-api` (the Moomoo SDK) and `python-telegram-bot` on top of
the normal TradingAgents install.

## 2. Set up OpenD (Moomoo's gateway)

1. Download **OpenD** from Moomoo's developer portal and launch it.
2. Log in with your Moomoo account (use a **paper** account to start).
3. Leave it running on `127.0.0.1:11111` (the default — don't change anything).
4. Keep OpenD running whenever you use this agent; it's the bridge to the broker.

## 3. Set up a Telegram bot (BotFather)

1. In Telegram, message **@BotFather** and send `/newbot`.
2. Follow the prompts; it gives you a **token** like `123456:ABC...`.
3. Send your new bot a message (any message — this lets it message you).
4. Get your **chat id** — a number. The quick way: message
   **@userinfobot** (it replies with your id), or run the agent once and watch
   its error message.

## 4. Configure

Put secrets in a `.env` file at the repo root (it's gitignored — **never commit
the token or chat id**):

```bash
# Required — from steps 2 and 3
EXECUTION_AGENT_TELEGRAM_BOT_TOKEN=123456:ABC-your-token
EXECUTION_AGENT_TELEGRAM_CHAT_ID=123456789

# Optional — these already have safe defaults
EXECUTION_AGENT_MOOMOO_HOST=127.0.0.1
EXECUTION_AGENT_MOOMOO_PORT=11111
EXECUTION_AGENT_TRADING_ENV=SIMULATE        # SIMULATE (paper) or REAL
EXECUTION_AGENT_APPROVAL_TIMEOUT_SECONDS=300
```

Every setting is optional except the two Telegram values.

## 5. Run it

```bash
source .venv/bin/activate
czagent run NVDA --date 2026-11-12
```

What happens, in order:

1. It runs the TradingAgents research for `NVDA`.
2. If the decision is Buy/Overweight/Sell/Underweight, it messages your phone:
   *"TradingAgents says BUY NVDA … How much do you want to trade?"*
3. You reply with a number — `500` (dollars) or `10 shares`. If you type a bare
   number, it asks a follow-up: **💵 Dollars** or **🔢 Shares**.
4. It shows the final order and an **Approve / Reject** pair of buttons.
5. **Approve** → it places the order on Moomoo (paper by default). **Reject** or
   no reply → nothing happens.

Useful flags:

| Flag | What it does |
|---|---|
| `--decide-only` | Run the research and print the decision — no Telegram, no order. |
| `--dry-run` | Run the whole approval flow but **do not** place the order. |
| `--portfolio book.json` | Feed in your positions so a SELL is capped at what you hold. |
| `--date YYYY-MM-DD` | The analysis date (required). |

## Run it from your phone (recommended)

Instead of typing the command each time, start a **persistent bot** and leave the
laptop running:

```bash
source .venv/bin/activate
czagent serve
```

(That first line activates the environment — skip it if your prompt already
shows `(.venv)`.)

Now you drive everything from Telegram:

1. Send a ticker — `NVDA`, `0700.HK`, or `/run NVDA`. The bot researches it,
   posting a one-line update on Telegram as each phase finishes (📊 market → 💬
   sentiment → 📰 news → 🏦 fundamentals → ⚖️ debate → 🎯 trader → 🛡️ risk →
   ✅ final), then replies with the call and summary **plus tap-able buttons**:
   ▶️ Execute and 💬 Ask. It **saves** the result but does **not** trade yet.
2. Tap **▶️ Execute** (or send `/execute`, or `/execute NVDA` for a specific
   ticker). The bot then asks *"How much?"* and dollars/shares, then
   **Approve / Reject**.
3. Tap **Approve** to place the order; the bot then goes back to listening for
   the next ticker.

`/execute <ticker>` refuses a ticker that hasn't been researched yet — send the
ticker first, then execute it. One trade runs at a time. To stop the bot, press
`Ctrl-C` in the laptop terminal. This is the same flow as `run` — the only
difference is *where* you start it.

### Your assistant commands

Send `/help` any time for this list in chat:

| Command | What it does |
|---|---|
| `/status` (or `/report`) | Account report: cash, total assets, each position with **profit/loss**, open orders, today's research, the watchlist. |
| `/orders` | Open (resting) orders, each with a tap-to-cancel ❌ button. |
| `/cancel <id>` | Cancel one resting order by its id. |
| `/watchlist` | Show the `/auto` watchlist. |
| `/watchlist add AMD` | Add a ticker — takes effect on the **next** `/auto`, no restart. |
| `/watchlist remove AMD` | Drop a ticker. |
| `/help` | The full command list. |

The watchlist you edit from the phone is stored in
`~/.tradingagents/execution/watchlist.json`; the `.env`
`EXECUTION_AGENT_WATCHLIST` only *seeds* it the first time.

### Hands-off mode: schedule + fill alerts

Two things run quietly in the background while the bot is up:

- **Daily scheduled `/auto`** — set `EXECUTION_AGENT_AUTO_SCHEDULE=09:30` in
  `.env` and the bot runs `/auto` by itself at that time each day. The scheduled
  run is **quiet**: no research chatter — you only get a message for orders
  placed, errors, and the final plan. If you already ran `/auto` that morning,
  the scheduled run skips (nothing is researched twice).
  `EXECUTION_AGENT_AUTO_SCHEDULE_BUDGET` sets how much new money it may deploy
  (0 = manage-only: sell/trim/hold, never buy).
- **Fill alerts** — the bot checks the broker every couple of minutes and
  messages you 🎉 when a resting limit/stop order actually fills, so you don't
  have to watch the market.

### Fully-automatic `/auto`

`/auto` is a **daily-budget manager**. It reads your account, researches the
universe, and decides buy/sell by itself — *sized by the research's
conviction*, not a fixed amount. On **paper** it places every decided trade
immediately, with no approval step. On **🔴 REAL**, deciding autonomously is
one thing — spending real money autonomously is another, so every trade it
wants to place is sent to you as an **Approve / Reject** tap first, exactly
like `/execute`; a timeout or Reject skips that trade (never a silent yes).
Protective stop-losses and cancelling a superseded resting order aren't
re-approved — they only reduce risk on a position you already approved into.

- **`/auto`** — "manage my account": research the watchlist + holdings and
  sell/trim/hold per the analysis, but deploy **no new cash**.
- **`/auto 500`** — "I have $500 today": split that budget across the buy ideas
  by conviction (`Buy` earns twice an `Overweight`) and buy at the trader's
  entry price as a limit order.

The **"what to buy" list** is your *watchlist* (`.env` `EXECUTION_AGENT_WATCHLIST`)
**plus** your *current holdings*, read live from the account. So `/auto` manages
what you own and, when you give it a budget, deploys the new cash across the
watchlist ideas.

The reply itself is deliberately terse — a one-line verdict per ticker in a
table, nothing else:

```
Ticker  Verdict
------  -----------
NVDA    Sell
AAPL    Hold
WDC     Buy @ 170.50
INTC    Buy @ 64.56?
```

`Buy`/`Sell` alone means the order already executed (a market fill). `Buy @
X`/`Sell @ X` — **no** `?` — means a real resting LIMIT order is sitting on
Moomoo right now; it fires automatically the instant price reaches `X`,
nothing further needed from you. `Buy @ X?`/`Sell @ X?` — **with** the `?` —
means **nothing is set anywhere**: it's only the price that *would* trigger a
buy/sell once you give `/auto` a budget, a live price becomes available, or
you approve it on REAL. `Not worth` means a Sell/Underweight call on a ticker
you don't hold, so there's nothing to act on. For the reasoning behind any of
these, ask `/chat <ticker> ...` — the full analyst reports and debate live
there, not in the summary.

```mermaid
flowchart TD
    Start([You: /auto — or /auto 500]) --> Read[Read account: cash + positions]
    Read --> Universe[Universe = watchlist from .env<br/>+ current holdings]
    Universe --> Research[Research each ticker]
    Research --> Rating{Rating?}
    Rating -->|Buy / Overweight| Budget{Budget given?}
    Budget -->|yes| Split[Split budget by conviction<br/>Buy 2x, Overweight 1x]
    Split --> Buy[BUY at entry price<br/>LIMIT — GTC or DAY by time horizon]
    Budget -->|no| NoBuy[No new cash — skip buy]
    Rating -->|Sell| Sell[SELL full exit at target]
    Rating -->|Underweight| Trim[SELL a trim at target]
    Rating -->|Hold / REVIEW| Hold[No order]
    Buy --> Bracket[Ensure bracket: stop-loss + take-profit<br/>sized to whatever is actually held]
    Hold --> Bracket
    NoBuy --> Bracket
    Sell --> Save[Save today's verdict]
    Trim --> Save
    Bracket --> Save
```

| Rating | Action |
|---|---|
| Buy / Overweight | BUY — split of the budget (Buy 2×, Overweight 1×) |
| Hold | no order (unless `AUTO_BUY_HOLD_PULLBACK` — see below) |
| Underweight | SELL a **trim** (default half — see `AUTO_UNDERWEIGHT_TRIM`) |
| Sell | SELL a **full exit** |

It follows the research's own levels — buy at the trader's entry price, sell
at the portfolio manager's price target — as limit orders. A **buy** with no
usable entry price falls back to a plain MARKET order. A **sell** never does:
with no usable target, it places a LIMIT a small buffer
(`AUTO_MARKET_SELL_BUFFER`, default 2%) below the live price instead of a raw
MARKET order — it still fills almost immediately in normal conditions, but
can't be dumped far below where the stock is actually trading into a bad or
illiquid print. (A buy has no equivalent concern — paying slightly more than
intended on an unprotected entry isn't the same risk as selling into a crash.)

### Opt-in: buy the pullback on a Hold

A `Hold` rating means "don't buy" by default — no amount of budget changes
that. But the trader sometimes states an entry price *even on a Hold* (e.g.
"I'd get interested below $340"), and setting
`EXECUTION_AGENT_AUTO_BUY_HOLD_PULLBACK=true` tells `/auto` to act on that: it
places a low-conviction resting **LIMIT** buy at that entry price for a
Hold-rated ticker you don't already hold — **only** when that price is
genuinely below the live price (a real pullback, not a level that would fill
immediately). It never falls back to MARKET, since buying at today's price
would defeat the whole point of waiting. This weighs half as much as an
Overweight when splitting the budget, so a genuine Buy/Overweight call always
gets priority. Off by default — the LLM doesn't reliably mean "buy here on a
dip" every time it states a number on a Hold, so this is your call to turn
on, not the default reading of that field.

### Protective bracket: stop-loss + take-profit, on autopilot

Every `/auto` run checks **every ticker it currently holds** — not just ones
it buys that run — and makes sure a full bracket is resting on Moomoo:

- a **stop-loss** (`STOP`, GTC) at the trader's stop level, and
- a **take-profit** (limit `SELL`, GTC) at the portfolio manager's price
  target,

both sized to whatever is actually held. This covers a position bought via
`/auto` itself, one bought interactively through `/execute`, or one you held
before this feature existed — the first `/auto` run after you have it will
backfill the bracket onto it. A pending limit buy that hasn't filled yet has
nothing to protect, so the bracket waits for a later run once it has.

**Both orders sit directly on Moomoo, not inside this bot.** Moomoo's own
matching engine fires them the instant price crosses the level — completely
independent of whether the bot is running. Moomoo has no native "one cancels
other" bracket type, though, so once one side fires the other is left resting
against shares that no longer exist; the bot's background fill-watcher (polls
every couple of minutes) notices and cancels that now-stale sibling, sending
you a message. If the bot happens to be offline when a side fires, the sale
still goes through fine on Moomoo's end — only the sibling cleanup is delayed
until the bot is back and polls again.

On **🔴 REAL**, the bracket is placed/replaced automatically, without an
Approve tap — it only manages risk on a position you already approved into
(sets a floor, locks in a gain), never takes on new exposure, unlike a fresh
BUY.

**Fractional dust positions** (a leftover like `0.3135` shares from a split or
spin-off) are handled, not skipped: an `Underweight` trim that floors to zero
units on a sub-1-share position reads as `Hold`, not `Not worth` — you still
hold it — and the bracket/full-exit sizing sends that exact fractional
quantity to Moomoo rather than truncating it away. Whether Moomoo's own
backend accepts a fractional order quantity depends on your account's
fractional-trading entitlement; a rejection shows up in the audit log and that
side of the bracket simply stays unset, same as any other order rejection.

One current limitation: the take-profit always exits the **full** position at
the target price — there's no partial "sell half here, let the rest ride"
split, because today's research only ever gives one target price, not a
scale-out plan. Teaching the LLM to produce its own scale-out plan would be a
bigger change to the core research pipeline, not just this execution layer.

Limit orders rest on Moomoo's side, so they survive the bot being offline — and
the agent decides how long the *entry* order rests based on the research's own
**Time Horizon**: a long call ("3-6 months") places a **GTC** order (rests
until filled or cancelled), a short call ("1-2 weeks") places a **DAY** order.
The bracket is always **GTC**. Because GTC orders can linger, each fresh run
first **cancels the prior day's open orders** on each ticker before placing
new ones (including the previous bracket, which is always replaced fresh each
run), so a stale limit can't double-fill or fire against a superseded verdict.

**Paper (SIMULATE) quirk**: Moomoo's paper environment rejects GTC outright
("Paper trading does not support GTC orders"). Every GTC request is silently
downgraded to DAY when not trading REAL, so this never fails — it just means
a paper order dies at that day's close regardless of the research's time
horizon, which costs nothing functionally since `/auto` already cancels and
reissues every order fresh on each run anyway.

The verdict is saved to `~/.tradingagents/execution/auto_plan.json`, keyed by
date (with the day's `budget`). Run `/auto` again the same day and every ticker
still goes through the decide + bracket-ensure step — that's what keeps the
protective bracket fresh even on a run where nothing else changed — but the
research itself is never re-paid for: a ticker already researched today (by
`/research` or an earlier `/auto`) reuses that cached result for free. A newly
added watchlist ticker gets freshly researched right alongside the reused
ones, so `/watchlist add AMD` mid-day takes effect on the very next `/auto`,
not just tomorrow. On a fresh day everything is researched from scratch.

Several guards keep LLM token costs down:

- **Same-day reuse everywhere** — a ticker researched today (by `/research` or
  `/auto`) is never re-researched the same day unless you force it with `fresh`.
  Re-deciding and refreshing its bracket on a later same-day `/auto` call is
  cheap (broker calls only, no LLM) and happens regardless.
- **Failed tickers aren't retried** — a ticker that errored today is skipped by
  later same-day `/auto` runs instead of re-paying for the same failure; a new
  day retries it naturally.
- **Terse progress** — `/auto` sends one line per ticker, not the ~9 per-phase
  updates each research produces (a 6-ticker scan would be ~50 messages).
- **Capped chat memory** — `/chat` replays only the last few Q&A turns
  (`EXECUTION_AGENT_CHAT_HISTORY_TURNS`, default 8) into each prompt, so a long
  conversation doesn't make every question costlier than the last.
Separately, if you already researched a ticker interactively today (e.g. a
morning `/run NVDA`), `/auto` reuses that same-day result instead of re-running
the full pipeline — it still fetches a fresh live quote to size the order. That
shared result lives in `~/.tradingagents/execution/research_history.json`.
`/auto`'s own research is saved to that same store too, so `/chat` can answer
questions about any ticker `/auto` looked at — including cross-ticker ones like
"how does NVDA compare to AAPL?" (`/chat` shows the full research for the ticker
you ask about and a one-line digest for the rest of today's runs).

**Scheduled `/auto` on REAL** asks for approval exactly the same way: each
trade the scheduled run wants to place still waits for your tap, it just does
so quietly (no "researching…" chatter) — so a 🔴 REAL morning schedule queues
up approval prompts for you to tap through instead of trading unsupervised
while you're asleep.

```bash
# Optional — seeds the watchlist on first use (then /watchlist edits rule)
EXECUTION_AGENT_WATCHLIST=NVDA,AAPL

# Optional — an "Underweight" sell trims to this fraction of the position
EXECUTION_AGENT_AUTO_UNDERWEIGHT_TRIM=0.50

# Optional — run /auto automatically every day at this time (quiet reporting)
EXECUTION_AGENT_AUTO_SCHEDULE=09:30
EXECUTION_AGENT_AUTO_SCHEDULE_BUDGET=0
```

## How it maps decisions to orders

| TradingAgents rating | Action |
|---|---|
| Buy / Overweight | BUY |
| Sell / Underweight | SELL |
| Hold | no order |
| REVIEW | never trade |

Orders are **market** orders in the interactive flow — the quantity is the only
number sent, and *you* choose the quantity in chat. The agent never parses
numbers out of the LLM's prose there. `/auto` is the exception: it places
**limit** orders at the research's own structured levels (entry price / price
target), since those come from the trader and portfolio-manager schemas rather
than free text.

## Supported symbols

Stocks and ETFs on these markets map automatically:

| You type | Moomoo code |
|---|---|
| `NVDA` | `US.NVDA` |
| `0700.HK` | `HK.00700` |
| `600519.SS` | `SH.600519` |

Crypto, forex, futures, and indices are rejected (Moomoo conventions differ).

## Safety defaults (not optional)

- **Paper first**: `TRADING_ENV=SIMULATE` by default. `REAL` requires you to
  change it explicitly *and* have a live Moomoo account unlocked in OpenD.
- **Approve is opt-in**: timeout or Reject never places an order.
- **REVIEW is skipped**: an unreadable decision is never traded.
- **Secrets never logged**: the audit trail below records decisions and orders,
  never your token or chat id.

## Audit log

Every step is written to `~/.tradingagents/execution/audit.jsonl` — one JSON
object per line (`decision`, `amount`, `proposal`, `approval`, `order`, `error`).
Override the path with `EXECUTION_AGENT_AUDIT_PATH`.

## Tests

```bash
pytest tests/test_execution_agent_*.py
```

The unit tests block the network, so the Moomoo and Telegram SDKs are faked — no
OpenD or Telegram needed to run them.
