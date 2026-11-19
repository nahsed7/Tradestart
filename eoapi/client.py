"""Thread-based websocket client for Expert Option.

Rewritten from ChipaDevTeam/ExpertOptionApi
(https://github.com/ChipaDevTeam/ExpertOptionApi, MIT License, Copyright (c) 2023 Vigo Walker).
The wire protocol (endpoints, action names, message shapes) comes from that project.

Changes from the original:
  * one self-contained module, no global state, no 5-second sleeps on every request
  * TLS certificates are verified, and the token is never logged
  * automatic reconnect across regions with backoff, plus a keep-alive ping thread
  * candles and ticks are parsed into per-asset stores that you can read at any time
  * request/response matching by `ns`, so you can wait for an answer with a timeout
  * demo account is the default for buy()

This is an unofficial client. Expert Option can change its protocol at any time, and
using unofficial clients may be against their terms. Use a demo account.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from collections import defaultdict, deque

import websocket

log = logging.getLogger("eoapi")

REGIONS = {
    "EUROPE": "wss://fr24g1eu.expertoption.com/",
    "INDIA": "wss://fr24g1in.expertoption.com/",
    "HONG_KONG": "wss://fr24g1hk.expertoption.com/",
    "SINGAPORE": "wss://fr24g1sg.expertoption.com/",
    "UNITED_STATES": "wss://fr24g1us.expertoption.com/",
}
ORIGIN = "https://app.expertoption.com"
PROTOCOL_VERSION = 18
PING_EVERY = 5


class ExpertOptionClient:
    def __init__(self, token: str, region: str = "EUROPE", demo: bool = True, on_message=None):
        if region not in REGIONS:
            raise ValueError(f"region must be one of {list(REGIONS)}")
        self.token = token
        self.region = region
        self.demo = demo
        self.on_message = on_message

        self._ws: websocket.WebSocketApp | None = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._ns = 100
        self._waiters: dict[str, list] = {}
        self._subscriptions: dict[int, tuple] = {}

        self.profile: dict | None = None
        self.assets: dict[int, dict] = {}
        self.last_by_action: dict[str, dict] = {}
        self.errors: deque = deque(maxlen=50)
        self.connected_url: str | None = None
        # (asset_id, tf) -> {timestamp: [open, high, low, close]}
        self._candles: dict[tuple, dict] = defaultdict(dict)
        # asset_id -> deque[(timestamp, price)]
        self._ticks: dict[int, deque] = defaultdict(lambda: deque(maxlen=20_000))

    # ------------------------------------------------------------ connection
    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def connect(self, timeout: float = 15) -> "ExpertOptionClient":
        self._stop.clear()
        threading.Thread(target=self._run, name="eoapi-ws", daemon=True).start()
        threading.Thread(target=self._ping_loop, name="eoapi-ping", daemon=True).start()
        if not self._connected.wait(timeout):
            raise ConnectionError("Could not connect to Expert Option (timeout)")
        return self

    def close(self):
        self._stop.set()
        if self._ws:
            self._ws.close()

    def _run(self):
        order = [self.region] + [r for r in REGIONS if r != self.region]
        attempt, backoff = 0, 1
        while not self._stop.is_set():
            url = REGIONS[order[attempt % len(order)]]
            self._ws = websocket.WebSocketApp(
                url, on_open=self._on_open, on_message=self._on_message,
                on_error=lambda _ws, e: log.warning("ws error: %s", e),
                on_close=lambda *_: self._connected.clear())
            started = time.time()
            try:
                self._ws.run_forever(origin=ORIGIN, ping_interval=0, skip_utf8_validation=True)
            except Exception as e:  # noqa: BLE001
                log.warning("ws crashed: %s", e)
            self._connected.clear()
            if self._stop.is_set():
                break
            if time.time() - started > 60:      # it was a healthy connection
                backoff = 1
            else:
                attempt += 1
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _on_open(self, ws):
        self.connected_url = ws.url
        self._connected.set()
        log.info("connected to %s", ws.url)
        self.send("multipleAction", {"actions": [
            self._payload("profile"),
            self._payload("assets", {"mode": ["vanilla", "binary"], "subscribeMode": ["vanilla"]}),
            self._payload("openOptions"),
            self._payload("setTimeZone", {"timeZone": 0}),
        ]}, ns="_common")
        if self.demo:
            self.set_demo()
        for aid, tfs in list(self._subscriptions.items()):
            self._send_subscribe(aid, tfs)

    def _ping_loop(self):
        while not self._stop.is_set():
            time.sleep(PING_EVERY)
            if self.is_connected:
                try:
                    self.send("ping", {}, ns="_", v=23)
                except Exception as e:  # noqa: BLE001
                    log.debug("ping failed: %s", e)

    # ------------------------------------------------------------ send / receive
    def _payload(self, action, message=None, ns=None, v=PROTOCOL_VERSION):
        return {"action": action, "message": message, "token": self.token, "ns": ns, "v": v}

    def _next_ns(self) -> str:
        with self._state_lock:
            self._ns += 1
            return str(self._ns)

    def send(self, action: str, message=None, ns=None, wait: bool = False,
             timeout: float = 10, v: int = PROTOCOL_VERSION):
        """Send one request. With wait=True, return the reply that has the same ns (or None)."""
        if not self.is_connected or self._ws is None:
            raise ConnectionError("not connected")
        ns = ns if ns is not None else self._next_ns()
        waiter = None
        if wait:
            waiter = [threading.Event(), None]
            self._waiters[str(ns)] = waiter
        data = urllib.parse.quote(json.dumps(self._payload(action, message, ns, v)))
        with self._send_lock:
            self._ws.send(data.encode("utf-8"), opcode=websocket.ABNF.OPCODE_BINARY)
        if waiter:
            waiter[0].wait(timeout)
            self._waiters.pop(str(ns), None)
            return waiter[1]
        return ns

    def _on_message(self, _ws, raw):
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        try:
            msg = json.loads(raw)
        except ValueError:
            try:
                msg = json.loads(urllib.parse.unquote(raw))
            except ValueError:
                log.debug("non-JSON message: %.200s", raw)
                return
        self._dispatch(msg)

    def _dispatch(self, msg: dict):
        if not isinstance(msg, dict):
            return
        action = msg.get("action")
        body = msg.get("message")
        if action == "multipleAction" and isinstance(body, dict):
            for sub in body.get("actions", []):
                self._dispatch(sub)
        if action:
            self.last_by_action[action] = msg
        if action == "profile":
            self.profile = body
        elif action == "assets":
            self._parse_assets(body)
        elif action in ("candles", "assetHistoryCandles", "subscribeCandles"):
            self._parse_candles(body)
        elif action == "error":
            self.errors.append((time.time(), body))
        ns = msg.get("ns")
        if ns is not None and str(ns) in self._waiters:
            w = self._waiters[str(ns)]
            w[1] = msg
            w[0].set()
        if self.on_message:
            try:
                self.on_message(msg)
            except Exception:  # noqa: BLE001
                log.exception("on_message callback failed")

    # ------------------------------------------------------------ parsing
    def _parse_assets(self, body):
        items = body.get("assets", []) if isinstance(body, dict) else body or []
        for a in items:
            if isinstance(a, dict) and "id" in a:
                self.assets[int(a["id"])] = a

    def _parse_candles(self, body):
        """Shape: {"candles": [{"assetId", "tf", "periods": [[ts, [[o,h,l,c,...], ...]], ...]}]}"""
        if not isinstance(body, dict):
            return
        for block in body.get("candles", []) or []:
            if not isinstance(block, dict):
                continue
            aid = block.get("assetId", block.get("asset_id", block.get("assetid")))
            if aid is None:
                continue
            aid, tf = int(aid), int(block.get("tf", 0) or 0)
            for period in block.get("periods", []) or []:
                if not isinstance(period, (list, tuple)) or len(period) < 2:
                    continue
                ts, rows = float(period[0]), period[1]
                if not isinstance(rows, (list, tuple)):
                    rows = [rows]
                for j, row in enumerate(rows):
                    if tf == 0:                      # tick stream
                        p = row[-1] if isinstance(row, (list, tuple)) else row
                        if isinstance(p, (int, float)):
                            self._ticks[aid].append((ts + j * 0.001, float(p)))
                    elif isinstance(row, (list, tuple)) and len(row) >= 4:
                        self._candles[(aid, tf)][ts + j * tf] = [float(v) for v in row[:4]]

    # ------------------------------------------------------------ public API
    def set_demo(self, demo: bool = True):
        self.demo = demo
        return self.send("setContext", {"is_demo": 1 if demo else 0}, ns="_common")

    def asset_name(self, asset_id: int) -> str:
        a = self.assets.get(int(asset_id), {})
        return a.get("name") or a.get("symbol") or f"Asset #{asset_id}"

    def subscribe_candles(self, asset_id: int, timeframes=(0, 5)):
        self._subscriptions[int(asset_id)] = tuple(timeframes)
        if self.is_connected:
            self._send_subscribe(int(asset_id), tuple(timeframes))

    def _send_subscribe(self, asset_id, timeframes):
        self.send("subscribeCandles", {"assets": [{"id": asset_id, "timeframes": list(timeframes)}],
                                       "modes": ["vanilla"]})

    def request_history(self, asset_id: int, tf: int = 5, seconds: int = 900, chunk: int = 300):
        """Ask for past candles; they arrive asynchronously into the candle store."""
        end = int(time.time()) // tf * tf
        periods, start = [], end - seconds
        while start < end:
            periods.append([start, min(start + chunk, end)])
            start += chunk
        return self.send("assetHistoryCandles",
                         {"assetid": int(asset_id), "periods": periods, "timeframes": [tf]})

    def get_candles(self, asset_id: int, tf: int = 5) -> list[tuple]:
        """Sorted list of (timestamp, open, high, low, close)."""
        store = self._candles.get((int(asset_id), tf), {})
        return [(t, *store[t]) for t in sorted(list(store))]

    def get_ticks(self, asset_id: int) -> list[tuple]:
        return list(self._ticks.get(int(asset_id), ()))

    def last_price(self, asset_id: int):
        """(price, timestamp) from the newest tick or candle, or None."""
        ticks = self._ticks.get(int(asset_id))
        best = ticks[-1] if ticks else None
        for (aid, _tf), store in list(self._candles.items()):
            if aid == int(asset_id) and store:
                t = max(store)
                if best is None or t > best[0]:
                    best = (t, store[t][3])
        return (best[1], best[0]) if best else None

    def buy(self, asset_id: int, direction: str, amount: float, duration: int = 60,
            demo: bool = True, timeout: float = 10):
        """Place an option. direction: "call"/"up" or "put"/"down". Returns the server reply."""
        kind = {"up": "call", "down": "put"}.get(direction, direction)
        if kind not in ("call", "put"):
            raise ValueError("direction must be call/put/up/down")
        now = int(time.time())
        expiration = (now // duration + 1) * duration
        if expiration - now < 15:
            expiration += duration
        if demo != self.demo:
            self.set_demo(demo)
        return self.send("buyOption", {
            "type": kind, "amount": amount, "assetid": int(asset_id), "strike_time": now,
            "expiration_time": expiration, "is_demo": 1 if demo else 0, "rateIndex": 1,
        }, wait=True, timeout=timeout)
