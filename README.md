# Live Market Chart

A Streamlit app with a live trading-style chart and a **paper-trading demo account** (default $50,000)
that runs against **real market prices**. It's for research: place UP/DOWN trades with fixed expiry
times, then review your win rate and P&L or export the history as CSV.

No real money is involved.

## Data sources

| Market | Source | Resolution |
|---|---|---|
| Crypto (BTC, ETH, SOL, BNB, XRP) | Binance public market data (Binance.US as fallback) | real-time, 1-second candles |
| Forex, gold, stocks, S&P 500 | Yahoo Finance chart API | quotes every few seconds, some markets delayed |
| Expert Option assets (optional) | bundled `eoapi` client, needs your token | broker quotes, 5-second candles |

## Features

- Area or candlestick chart with volume, a live price line and room to the right of the price
- Timeframes from 1s to 15m, depending on the source
- UP/DOWN trades with 30s–15m expiry, drawn on the chart with entry and finish lines
- Crypto trades settle on Binance's exact 1-second close at expiry
- Trading is disabled while a market is closed
- Balance and trades saved to `paper_account.json`; reset to any balance from the sidebar
- History with win rate, net P&L and CSV download

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud

1. Push this folder to a GitHub repository.
2. On share.streamlit.io choose **Create app**, then pick the repo, branch `main` and file `app.py`.
3. Optional: under **App settings → Secrets**, add your Expert Option token (see
   `.streamlit/secrets.toml.example`).

On Streamlit Cloud the file system is temporary, so `paper_account.json` resets whenever the app
restarts. Download your history CSV to keep it.

## `eoapi`: Expert Option client

`eoapi/` is our rewrite of [ChipaDevTeam/ExpertOptionApi](https://github.com/ChipaDevTeam/ExpertOptionApi).
It fixes the broken imports, the 5-second blocking on every call, the disabled TLS checks and the
token logging. It also adds reconnect, ping, `ns` request matching and parsed candle stores.

```python
from eoapi import ExpertOptionClient

eo = ExpertOptionClient(token="YOUR_TOKEN", region="EUROPE").connect()
eo.subscribe_candles(240, timeframes=(0, 5))
eo.request_history(240, tf=5, seconds=900)
print(eo.last_price(240), eo.get_candles(240, 5)[-3:])
eo.buy(240, "up", amount=10, duration=60, demo=True)
```

This is an unofficial client, and Expert Option can change its protocol at any time. Use a demo account.

## Credits

- Expert Option protocol and original client: **ChipaDevTeam / Vigo Walker**,
  [ExpertOptionApi](https://github.com/ChipaDevTeam/ExpertOptionApi), MIT License. The original
  license is kept in `eoapi/LICENSE-ExpertOptionApi`.
- Market data: Binance public API and Yahoo Finance. This project isn't affiliated with Binance,
  Yahoo or Expert Option.

## License

MIT, see `LICENSE`.
