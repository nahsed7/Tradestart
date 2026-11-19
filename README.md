# Price Graph Learning Lab

A beginner-friendly Streamlit app for learning to read a price-like time series.

## Features
- Illustrative sample data (not real market prices)
- Upload your own CSV with `time` and `price` columns
- Explore a selectable recent observation window
- See upward, downward, and unchanged steps
- Visualize changes between consecutive observations
- Group observations into larger intervals

This is an educational visualization tool. It does not predict prices, issue trading signals, connect to brokers, or execute trades.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud
1. Create a GitHub repository.
2. Add `app.py`, `requirements.txt`, and `README.md`.
3. In Streamlit Community Cloud, choose **New app**.
4. Select your repository, branch, and `app.py`.
5. Deploy.

## CSV format

Your CSV must have columns named `time` and `price`.

```csv
time,price
2026-01-01 09:00:00,100.25
2026-01-01 09:00:01,100.31
2026-01-01 09:00:02,100.28
```
