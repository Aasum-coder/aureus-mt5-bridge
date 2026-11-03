"""AUREUS bridge. Runs on the Windows VPS, next to a logged-in MetaTrader 5.

PowerShell, from this folder:

    pip install MetaTrader5
    $env:AUREUS_BRIDGE_KEY = "choose-a-long-secret"
    python mt5_bridge.py

Then send the public address http://VPS-IP:5000 and the same secret.
MetaTrader must stay open and logged in to the PU Prime account.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("AUREUS_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("AUREUS_BRIDGE_PORT", "5000"))
KEY = os.environ.get("AUREUS_BRIDGE_KEY", "").strip()
TOKENS: dict[str, float] = {}
TOKEN_TTL = 12 * 60 * 60

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


ONLY = os.environ.get("AUREUS_LOGIN", "33849261").strip()
CHOSEN: str | None = None


def terminal_paths() -> list[str]:
    found: list[str] = []
    forced = os.environ.get("MT5_PATH", "").strip()
    if forced:
        found.append(forced)
    try:
        import subprocess

        raw = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name = 'terminal64.exe'\" | Select-Object -ExpandProperty ExecutablePath",
            ],
            text=True,
            timeout=20,
        )
        for line in raw.splitlines():
            path = line.strip()
            if path and path not in found:
                found.append(path)
    except Exception:
        pass
    return found


def attach() -> None:
    """Connect the API to the AUREUS terminal only. shutdown() does not log Apex out."""
    global CHOSEN
    if mt5 is None:
        raise RuntimeError("MetaTrader5 is not installed. Run: pip install MetaTrader5")
    paths = terminal_paths() or [""]
    seen: list[str] = []
    for path in paths:
        mt5.shutdown()
        ok = mt5.initialize(path) if path else mt5.initialize()
        if not ok:
            continue
        info = mt5.account_info()
        if info is None:
            mt5.shutdown()
            continue
        seen.append(f"{info.login} ({path or 'default'})")
        if str(info.login) == ONLY:
            CHOSEN = path
            print(f"AUREUS locked to {info.login}. Other terminals are left logged in.")
            return
        mt5.shutdown()
    raise RuntimeError(
        f"AUREUS account {ONLY} was not found. Saw: {', '.join(seen) or 'no terminal'}. No order will be sent."
    )


def ensure() -> None:
    if mt5 is None:
        raise RuntimeError("MetaTrader5 is not installed. Run: pip install MetaTrader5")
    if CHOSEN is None:
        attach()
        return
    ok = mt5.initialize(CHOSEN) if CHOSEN else mt5.initialize()
    if not ok:
        raise RuntimeError(f"The AUREUS terminal is not running. {mt5.last_error()}")
    info = mt5.account_info()
    if info is None or str(info.login) != ONLY:
        raise RuntimeError(f"Refusing to trade {getattr(info, 'login', 'unknown')}. AUREUS only trades {ONLY}.")


def account():
    ensure()
    info = mt5.account_info()
    if info is None:
        raise RuntimeError("MetaTrader 5 has no logged-in account.")
    return info


def auth_ok(query: dict[str, list[str]], header: str | None) -> bool:
    if not KEY:
        return True
    sent = (query.get("key") or [""])[0]
    bearer = (header or "").removeprefix("Bearer ").strip()
    return secrets.compare_digest(sent or bearer, KEY)


def session_ok(query: dict[str, list[str]]) -> bool:
    token = (query.get("id") or [""])[0]
    issued = TOKENS.get(token)
    if not token or issued is None or time.time() - issued > TOKEN_TTL:
        return False
    TOKENS[token] = time.time()
    return True


def one(query: dict[str, list[str]], name: str, default: str = "") -> str:
    return (query.get(name) or [default])[0]


def stamp(epoch: float | int | None) -> str:
    if not epoch:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def quote_of(symbol: str) -> dict:
    ensure()
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Symbol {symbol} is not on this account.")
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or tick.bid <= 0 or tick.ask <= 0:
        raise RuntimeError(f"No tick for {symbol}.")
    when = int(getattr(tick, "time_msc", 0) or int(tick.time) * 1000)
    return {"bid": tick.bid, "ask": tick.ask, "time": when, "timeMsc": when, "symbol": symbol}


def symbol_row(symbol: str) -> dict:
    ensure()
    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Symbol {symbol} is not on this account.")
    return {
        "symbol": info.name,
        "point": info.point,
        "digits": info.digits,
        "stopsLevel": info.trade_stops_level,
        "freezeLevel": info.trade_freeze_level,
        "contractSize": info.trade_contract_size,
        "tickSize": info.trade_tick_size,
        "tickValue": info.trade_tick_value,
        "volumeMin": info.volume_min,
        "volumeMax": info.volume_max,
        "volumeStep": info.volume_step,
        "min": info.volume_min,
        "max": info.volume_max,
        "step": info.volume_step,
    }


def account_body() -> dict:
    info = account()
    kind = "demo" if info.trade_mode == 0 else "real"
    return {
        "login": info.login,
        "balance": info.balance,
        "equity": info.equity,
        "margin": info.margin,
        "freeMargin": info.margin_free,
        "currency": info.currency,
        "type": kind,
        "accountType": kind,
        "serverName": info.server,
        "company": info.company,
        "leverage": info.leverage,
    }


def positions() -> list[dict]:
    ensure()
    rows = mt5.positions_get() or []
    out = []
    for row in rows:
        out.append(
            {
                "ticket": row.ticket,
                "symbol": row.symbol,
                "volume": row.volume,
                "type": "Buy" if row.type == 0 else "Sell",
                "price": row.price_open,
                "openPrice": row.price_open,
                "stopLoss": row.sl,
                "takeProfit": row.tp,
                "profit": row.profit,
                "comment": row.comment,
                "openTime": stamp(row.time),
            }
        )
    return out


def filling(symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    mode = int(getattr(info, "filling_mode", 0) or 0)
    if mode & 1:
        return mt5.ORDER_FILLING_FOK
    if mode & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def send_market(query: dict[str, list[str]]) -> dict:
    ensure()
    symbol = one(query, "symbol")
    side = one(query, "operation", "Buy").lower()
    volume = float(one(query, "volume", "0.01"))
    sl = float(one(query, "stoploss", "0") or 0)
    tp = float(one(query, "takeprofit", "0") or 0)
    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError(f"No tick for {symbol}.")
    order_type = mt5.ORDER_TYPE_SELL if side == "sell" else mt5.ORDER_TYPE_BUY
    price = tick.bid if side == "sell" else tick.ask
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": int(one(query, "slippage", "40") or 40),
        "magic": 850610,
        "comment": one(query, "comment", "AUREUS")[:31],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling(symbol),
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        comment = getattr(result, "comment", "") if result else mt5.last_error()
        raise RuntimeError(f"Order rejected. {comment}")
    return {"ticket": result.order or result.deal, "symbol": symbol, "openPrice": result.price or price}


def modify(query: dict[str, list[str]]) -> dict:
    ensure()
    ticket = int(one(query, "ticket"))
    sl = float(one(query, "stoploss", "0") or 0)
    tp = float(one(query, "takeprofit", "0") or 0)
    rows = mt5.positions_get(ticket=ticket) or []
    if not rows:
        raise RuntimeError(f"Position {ticket} is not open.")
    position = rows[0]
    result = mt5.order_send(
        {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": ticket,
            "sl": sl,
            "tp": tp or position.tp,
        }
    )
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        comment = getattr(result, "comment", "") if result else mt5.last_error()
        raise RuntimeError(f"Stop was not changed. {comment}")
    return {"ticket": ticket, "code": 0}


def close_position(query: dict[str, list[str]]) -> dict:
    ensure()
    ticket = int(one(query, "ticket"))
    lots = float(one(query, "lots", "0") or 0)
    rows = mt5.positions_get(ticket=ticket) or []
    if not rows:
        raise RuntimeError(f"Position {ticket} is not open.")
    position = rows[0]
    volume = lots if lots > 0 else position.volume
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        raise RuntimeError("No tick for the close.")
    sell = position.type == 0
    result = mt5.order_send(
        {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_SELL if sell else mt5.ORDER_TYPE_BUY,
            "position": ticket,
            "price": tick.bid if sell else tick.ask,
            "deviation": 40,
            "magic": 850610,
            "comment": "AUREUS close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling(position.symbol),
        }
    )
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        comment = getattr(result, "comment", "") if result else mt5.last_error()
        raise RuntimeError(f"Close rejected. {comment}")
    return {"ticket": ticket, "price": result.price}


def history(query: dict[str, list[str]]) -> list[dict]:
    ensure()
    start = datetime.fromisoformat(one(query, "from").replace("Z", "+00:00"))
    end = datetime.fromisoformat(one(query, "to").replace("Z", "+00:00"))
    deals = mt5.history_deals_get(start, end) or []
    out = []
    for deal in deals:
        out.append(
            {
                "ticket": deal.position_id or deal.ticket,
                "deal": deal.ticket,
                "symbol": deal.symbol,
                "volume": deal.volume,
                "price": deal.price,
                "closePrice": deal.price,
                "profit": deal.profit,
                "commission": deal.commission,
                "swap": deal.swap,
                "comment": deal.comment,
                "closeTime": stamp(deal.time),
                "type": deal.type,
            }
        )
    return out


FRAME = {1: "TIMEFRAME_M1", 5: "TIMEFRAME_M5", 15: "TIMEFRAME_M15", 60: "TIMEFRAME_H1", 240: "TIMEFRAME_H4", 1440: "TIMEFRAME_D1"}


def bars(query: dict[str, list[str]]) -> list[dict]:
    ensure()
    symbol = one(query, "symbol")
    minutes = int(float(one(query, "timeFrame", "1") or 1))
    name = FRAME.get(minutes, "TIMEFRAME_M1")
    frame = getattr(mt5, name)
    start = datetime.fromisoformat(one(query, "from").replace("Z", "+00:00"))
    end = datetime.fromisoformat(one(query, "to").replace("Z", "+00:00"))
    mt5.symbol_select(symbol, True)
    rows = mt5.copy_rates_range(symbol, frame, start, end) or []
    return [
        {
            "time": int(row["time"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["tick_volume"]),
        }
        for row in rows
    ]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[aureus-bridge] {self.address_string()} {fmt % args}")

    def reply(self, status: int, body) -> None:
        raw = body if isinstance(body, str) else json.dumps(body)
        data = raw.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        if path != "/health" and not auth_ok(query, self.headers.get("Authorization")):
            self.reply(401, {"message": "Bridge key rejected."})
            return
        try:
            if path == "/health":
                info = account()
                self.reply(200, {"ok": True, "login": info.login, "server": info.server})
                return
            if path == "/Search":
                self.reply(200, [{"companyName": "PU Prime Ltd", "results": [{"name": "PUPrime-Live2", "access": []}]}])
                return
            if path in ("/Connect", "/ConnectEx"):
                info = account()
                login = one(query, "user")
                if login and str(info.login) != login:
                    self.reply(401, {"message": f"This terminal is logged into {info.login}, not {login}."})
                    return
                token = secrets.token_hex(16)
                TOKENS[token] = time.time()
                self.reply(200, token)
                return
            if not session_ok(query):
                self.reply(401, {"message": "Bridge session expired. Connect again."})
                return
            if path == "/GetQuote":
                self.reply(200, quote_of(one(query, "symbol", "XAUUSD")))
                return
            if path == "/SymbolList":
                ensure()
                names = [row.name for row in (mt5.symbols_get() or [])]
                self.reply(200, names)
                return
            if path == "/SymbolParams":
                self.reply(200, symbol_row(one(query, "symbol")))
                return
            if path in ("/AccountSummary", "/AccountDetails"):
                self.reply(200, account_body())
                return
            if path == "/OpenedOrders":
                self.reply(200, positions())
                return
            if path == "/OrderSendTask":
                self.reply(200, send_market(query))
                return
            if path == "/OrderModifyTask":
                self.reply(200, modify(query))
                return
            if path == "/OrderClose":
                self.reply(200, close_position(query))
                return
            if path == "/OrderHistory":
                self.reply(200, history(query))
                return
            if path == "/PriceHistory":
                self.reply(200, bars(query))
                return
            if path == "/ServerTimezone":
                self.reply(200, "180")
                return
            if path == "/IsTradeSession":
                self.reply(200, "true")
                return
            if path == "/SymbolSessions":
                self.reply(200, [])
                return
            if path == "/SubscribeMany":
                self.reply(200, {"ok": True})
                return
            self.reply(404, {"message": f"{path} is not on this bridge."})
        except Exception as error:
            self.reply(400, {"message": str(error)})


if __name__ == "__main__":
    try:
        attach()
    except Exception as exc:
        print(exc)
        raise SystemExit(1)
    print(f"AUREUS bridge on {HOST}:{PORT}. Trading only {ONLY}.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
