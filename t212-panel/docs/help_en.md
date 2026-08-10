# Snajper — user guide

A trading panel for Trading 212: manual orders (Warp Mode/Focus) plus three automated engines (Bot, Signal, EOD) that open and manage positions on their own.

## Account and login

- **Register** — `/auth/register`. After registering you get a **recovery code** — shown ONLY ONCE, save it immediately (a password manager or a secure offline place). If you lose both your password and this code, no one (including the app admin) can recover your encrypted API keys.
- **Forgot password** — `/auth/recover`, needs the recovery code from registration.
- Your T212 API keys are encrypted with your password (zero-knowledge) — no one but you can read them, not even with direct database access.

## First steps (from registration to a running engine)

1. **Register and save your recovery code** (see above) — without it you can't recover access after a forgotten password.

2. **Settings → API Keys → T212 key** (required to trade) — paste your key and secret from the T212 app (Settings → API (Beta) inside the T212 app), separately for demo and live accounts. The **DEMO/LIVE** toggle in the top bar shows and switches which account you're working on — **LIVE = real money**, red and always visible.

3. **Settings → API Keys → market data (Finnhub/Alpaca)** — **these are YOUR OWN keys, not the app admin's.** A free account at [finnhub.io](https://finnhub.io/register) and/or [alpaca.markets](https://alpaca.markets) is enough. The app works without them too (falls back to Yahoo Finance, which needs no key at all), but with your own keys, prices/charts are faster and fuller, without sharing a request quota with other users.

4. **IBKR (optional, advanced)** — this is NOT a simple key, but the address (host:port) of your OWN, self-hosted IB Gateway (a separate application from Interactive Brokers that must keep running). Without this field the app uses the shared gateway like before — this only affects candlestick charts on the instrument page, never trading decisions.

5. **Settings → Watchlist** (optional, for the manual Warp/Focus modes) — search for instruments you're interested in and add them to favorites (max 9 in the Warp Mode grid).

6. **Starting an automated engine** (Bot / Signal / EOD — the same pattern for each):
   - Go to the engine's page (link in the top bar).
   - In the **"Assets [engine]"** section, add at least one ticker + entry amount — without this, an active engine has nothing to buy.
   - (optional) adjust **"Risk settings"** — the defaults are safe to start with.
   - In the **"Activation"** section, enter your password and click **"Start"** — this ACTIVATES the engine (decrypts your T212 key in server memory for as long as the app keeps running, so it can place orders on its own).
   - The engine starts on the next cycle (up to ~60 seconds) — check the "ACTIVE" status in the banner at the top of the page and new entries in the **"Log"** at the bottom.

## Top navigation bar

On a phone the menu is a single horizontal, scrollable strip — swipe sideways to see more tabs. The theme toggle (light/dark), language switch (PL/EN), and T212 connection status stay pinned on the right, regardless of scrolling.

## Manual modes

### Warp Mode
A 3×3 grid of tiles with your favorite instruments — one-click BUY/SELL, LIMIT, STOP, STOP-LIMIT orders. Prices update live. Recent order history and open pending orders are shown alongside.

### Focus
Same as Warp Mode, but one larger tile at a time — more comfortable on a phone, or when you want to focus on a single position. Switch between grid tiles with arrows/buttons.

### Assets (Aktywa)
A full overview of your T212 portfolio — every position (not just bot-managed ones), value, profit/loss, and which engine (if any) manages each position. The baseline ("vs starting point") can be reset manually, e.g. after resetting a demo account.

### Virtual Pie
Private, virtual baskets — buy several stocks at once with set proportions (weights) in a single click, without paying a separate FX fee per individual order.

## Automated engines

All three run independently, each with its **own asset list** and **own risk settings**. Activation requires your password (so the app can decrypt your API key) — after an app restart they resume automatically, without asking for the password again.

### Bot (Micro-Grid)
A full loop: entry → buying more on dips (DCA) → trailing take-profit and stop-loss that follow the price up and protect profit. It can also take over managing a position you bought manually ("Hand to bot" on the instrument page).

### Signal
A separate strategy — enters a position when the RSI indicator shows an oversold condition within an uptrend. One entry per signal (no averaging in), stop-loss and take-profit are calculated right at entry.

### EOD (end of day)
Reacts to sudden, sharp price drops within a few minutes — the sharper the drop, the bigger the position. Can optionally force-close all positions near the end of the trading day.

**Release a position** — every engine-managed position has a "Release"/"Undo" button — the shares stay in your account, only that engine stops watching them (e.g. when you want to sell it yourself manually).

## Safety

- The **demo** account uses virtual money, safe for testing.
- The **live** account uses real money — the red LIVE indicator in the top bar is always visible so there's no doubt which account you're acting on.
- Weekend stop-loss suspension (if enabled) means positions are unprotected from Friday 21:00 to Monday 11:00 — a deliberate choice, not a bug.
- Error/important-event notifications are sent to Telegram.

## Language and theme

The **PL/EN** switch in the top bar (next to the theme icon) works even before you log in. Your choice is remembered in the browser, and for a logged-in user also on the account.
