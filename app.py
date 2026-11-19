import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px

st.set_page_config(page_title="Price Graph Learning Lab", page_icon="📈", layout="wide")

st.title("📈 Price Graph Learning Lab")
st.caption("An educational tool for exploring how a price line moves over time. It does not provide trading signals or connect to a broker.")

st.info("Start with the sample dataset, or upload your own CSV containing a time column and a price column. All analysis is descriptive and educational.")

@st.cache_data
def make_sample(n=600):
    rng = np.random.default_rng(17)
    changes = rng.normal(0, 0.12, n)
    # A deliberately illustrative series, not real market data.
    price = 100 + np.cumsum(changes)
    times = pd.date_range("2026-01-01 09:00:00", periods=n, freq="s")
    return pd.DataFrame({"time": times, "price": price})

with st.sidebar:
    st.header("Data")
    source = st.radio("Choose data source", ["Illustrative sample", "Upload CSV"])
    uploaded = None
    if source == "Upload CSV":
        uploaded = st.file_uploader("Upload CSV", type=["csv"])
    st.divider()
    st.caption("CSV format")
    st.code("time,price\\n2026-01-01 09:00:00,100.25\\n2026-01-01 09:00:01,100.31", language="text")

if source == "Illustrative sample":
    df = make_sample()
else:
    if uploaded is None:
        st.warning("Upload a CSV file to continue, or select Illustrative sample.")
        st.stop()
    try:
        raw = pd.read_csv(uploaded)
    except Exception as e:
        st.error(f"Could not read CSV: {e}")
        st.stop()
    if not {"time", "price"}.issubset(raw.columns):
        st.error("Your CSV must contain columns named 'time' and 'price'.")
        st.stop()
    df = raw[["time", "price"]].copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna().sort_values("time").reset_index(drop=True)
    if len(df) < 3:
        st.error("Please provide at least three valid observations.")
        st.stop()

st.subheader("1. Explore the price line")
window = st.slider("How many recent observations should be visible?", min_value=20, max_value=min(600, len(df)), value=min(150, len(df)), step=10)
view = df.tail(window).copy()
view["change"] = view["price"].diff()
view["direction"] = np.where(view["change"] > 0, "Up", np.where(view["change"] < 0, "Down", "Unchanged"))

fig = px.line(view, x="time", y="price", title="Price over time")
fig.update_traces(line={"width": 2})
fig.update_layout(xaxis_title="Time", yaxis_title="Price", hovermode="x unified")
st.plotly_chart(fig, use_container_width=True)

latest = view["price"].iloc[-1]
first = view["price"].iloc[0]
net_change = latest - first
pct_change = (net_change / first * 100) if first else np.nan
c1, c2, c3 = st.columns(3)
c1.metric("Latest observed value", f"{latest:.5f}")
c2.metric("Change across visible window", f"{net_change:+.5f}")
c3.metric("Percentage change across window", f"{pct_change:+.3f}%")

st.subheader("2. Understand the direction")
st.write("Each point is one recorded observation. The line joins observations in time order.")
up = int((view["change"] > 0).sum())
down = int((view["change"] < 0).sum())
flat = int((view["change"] == 0).sum())
a, b, c = st.columns(3)
a.metric("Steps up", up)
b.metric("Steps down", down)
c.metric("Unchanged steps", flat)

st.subheader("3. How quickly did it change?")
st.write("The change between consecutive observations is the new value minus the previous value.")
view["step_change"] = view["price"].diff()
change_fig = px.bar(view.dropna(), x="time", y="step_change", title="Change between consecutive observations")
change_fig.update_layout(xaxis_title="Time", yaxis_title="Price change", hovermode="x unified")
st.plotly_chart(change_fig, use_container_width=True)

st.subheader("4. Try a different time grouping")
st.caption("This groups observations into larger intervals for easier viewing. It does not predict future prices.")
if len(df) >= 4:
    grouping = st.select_slider("Group every number of observations", options=[1, 2, 5, 10, 20, 50], value=5)
    grouped = df.tail(window).copy()
    grouped["group"] = np.arange(len(grouped)) // grouping
    summary = grouped.groupby("group", as_index=False).agg(time=("time", "last"), price=("price", "last"))
    grouped_fig = px.line(summary, x="time", y="price", title=f"Last value in each group of {grouping} observations")
    grouped_fig.update_layout(xaxis_title="Time", yaxis_title="Price")
    st.plotly_chart(grouped_fig, use_container_width=True)

with st.expander("What should I learn from this page?"):
    st.markdown("""
    - **Upward movement:** a later observation is higher than an earlier one.
    - **Downward movement:** a later observation is lower.
    - **Change:** the difference between two consecutive observations.
    - **Time window:** the section of the recorded series you are looking at.
    - **Important limitation:** describing the past is not the same as predicting the future. A line that went up several times does not guarantee that it will keep going up.
    """)

st.caption("Educational only. Sample values are randomly generated and are not market quotes. No trading recommendations, forecasts, broker integration, or order execution.")
