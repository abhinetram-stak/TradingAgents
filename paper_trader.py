#!/usr/bin/env python3
"""
paper_trader.py — TradingAgents paper trading engine (NSE/BSE India)

Runs continuously during Indian market hours (NSE: 9:15 AM – 3:30 PM IST,
Mon–Fri). At the configured analysis time (default 9:20 AM IST) it runs
TradingAgents on each ticker, sizes positions based on the 5-tier rating,
and logs all paper orders. At 3:30 PM IST it prints an EOD P&L summary.
On Fridays it also prints a weekly summary.

State (positions, trades, P&L history) is persisted to paper_portfolio.json
so the process can be restarted without losing data.

Usage:
    uv run python paper_trader.py              # run live (leave it running)
    uv run python paper_trader.py --status     # show current portfolio & exit
    uv run python paper_trader.py --eod        # print EOD summary now & exit
    uv run python paper_trader.py --reset      # wipe portfolio back to starting capital
"""

import argparse
import atexit
import json
import logging
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from notifier import send_telegram, telegram_configured

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

# -- Configuration -------------------------------------------------------------

PAPER_CONFIG = {
    # NSE tickers — use .NS suffix for NSE, .BO for BSE
    "tickers": ["RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS"],

    # Notional starting capital in INR
    "starting_capital": 10_00_000.0,   # ₹10 lakhs

    # Capital allocated per ticker as a fraction of starting_capital.
    # 5 tickers × 0.18 = 90% deployed, 10% cash buffer.
    "position_size_pct": 0.18,

    # If False, Underweight/Sell ratings close any long but don't go short.
    # Set to True to allow short positions.
    "allow_short": False,

    # Day-trading mode: close ALL open positions at 3:25 PM IST every day.
    # False = swing mode (positions carry overnight until signal changes).
    "day_trading": True,

    # Time to run morning analysis (IST). 9:30 lets the NSE open (9:15) settle
    # and gives yfinance's ~15-min data delay time to publish today's bars.
    "analysis_time": "09:30",

    # OpenAI models
    "deep_think_llm": "gpt-4o",
    "quick_think_llm": "gpt-4o-mini",
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,

    # Risk controls — applied on every intraday price check
    "stop_loss_pct":        0.02,   # fixed floor  — close if price drops 2% from entry
    "trailing_stop_pct":    0.02,   # trailing      — close if price drops 2% from peak
    "take_profit_pct":      0.03,   # hard ceiling  — close if price rises 3% from entry

    # Seconds between intraday price checks (stop/TP monitoring)
    "price_check_interval": 300,    # 5 minutes

    # Telegram notifications. Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
    # in .env. Status updates are sent every 30 minutes while the bot runs.
    "notifications_enabled": os.getenv("PAPER_NOTIFICATIONS_ENABLED", "1").lower()
    in {"1", "true", "yes", "on"},
    "notification_interval_seconds": int(
        os.getenv("PAPER_NOTIFICATION_INTERVAL_SECONDS", "1800")
    ),

    # Deployment safety controls.
    "trading_mode": os.getenv("TRADING_MODE", "paper").lower(),
    "bot_enabled": os.getenv("BOT_ENABLED", "1").lower() in {"1", "true", "yes", "on"},

    # Persistence paths
    "data_dir": os.getenv("PAPER_DATA_DIR", "."),
    "state_file": os.getenv("PAPER_STATE_FILE", "paper_portfolio.json"),
    "log_file": os.getenv("PAPER_LOG_FILE", "paper_trades.log"),
    "lock_file": os.getenv("PAPER_LOCK_FILE", "paper_trader.lock"),
}

# Rating → signed position multiplier
# Full position_size_pct × multiplier gives notional allocation.
RATING_TO_SIGNAL = {
    "Buy":         1.0,
    "Overweight":  0.5,
    "Hold":        None,   # None = keep existing position unchanged
    "Underweight": -0.5,
    "Sell":        -1.0,
}

# -- Logging -------------------------------------------------------------------

def _setup_logging(log_file: str) -> None:
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )

log = logging.getLogger(__name__)


# -- Paths and process safety --------------------------------------------------

def _runtime_path(config_key: str) -> Path:
    path = Path(PAPER_CONFIG[config_key]).expanduser()
    if path.is_absolute():
        return path
    return Path(PAPER_CONFIG["data_dir"]).expanduser() / path


def _ensure_runtime_dirs() -> None:
    for key in ("state_file", "log_file", "lock_file"):
        _runtime_path(key).parent.mkdir(parents=True, exist_ok=True)


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            details = self.path.read_text(encoding="utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Another paper trader instance appears to be running. "
                f"Lock file: {self.path}. Details: {details}"
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"pid={os.getpid()}\nstarted={datetime.now(IST).isoformat()}\n")
        self.acquired = True
        atexit.register(self.release)

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not remove lock file %s: %s", self.path, exc)
        finally:
            self.acquired = False


def _assert_safe_to_run_bot() -> None:
    if PAPER_CONFIG["trading_mode"] != "paper":
        raise RuntimeError(
            "Refusing to run: TRADING_MODE must be set to 'paper' for this app."
        )
    if not PAPER_CONFIG["bot_enabled"]:
        raise RuntimeError("BOT_ENABLED is false; scheduled paper trading is disabled.")


# -- Notifications -------------------------------------------------------------

def _notify(message: str, *, quiet: bool = False) -> None:
    if PAPER_CONFIG.get("notifications_enabled", True):
        send_telegram(message, disable_notification=quiet)


def _format_inr(value: float | int | None) -> str:
    return f"Rs {float(value or 0):,.2f}"


def _position_stop(pos: dict, price: float | None = None) -> float:
    trail_pct = PAPER_CONFIG["trailing_stop_pct"]
    peak = float(pos.get("peak_price", price or pos.get("entry_price", 0)) or 0)
    direction = int(pos.get("direction", 1) or 1)
    if direction == 1:
        trailing_stop = peak * (1 - trail_pct)
        return max(float(pos.get("stop_loss_price", 0) or 0), trailing_stop)
    trailing_stop = peak * (1 + trail_pct)
    return min(float(pos.get("stop_loss_price", trailing_stop) or trailing_stop), trailing_stop)


def notify_position_status(portfolio: "Portfolio", prices: dict | None = None) -> None:
    prices = prices or get_prices(PAPER_CONFIG["tickers"], mode="last")
    total = portfolio.total_value(prices)
    pnl = total - portfolio.starting_capital
    pnl_pct = pnl / portfolio.starting_capital * 100 if portfolio.starting_capital else 0

    lines = [
        "Paper trader status",
        datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        f"Total value: {_format_inr(total)} ({pnl:+,.2f}, {pnl_pct:+.2f}%)",
        f"Cash: {_format_inr(portfolio.cash)}",
        f"Open positions: {len(portfolio.positions)}",
    ]

    if portfolio.positions:
        lines.append("")
        for ticker, pos in portfolio.positions.items():
            px = float(prices.get(ticker, pos["entry_price"]))
            direction = int(pos["direction"])
            side = "LONG" if direction == 1 else "SHORT"
            upnl = (px - pos["entry_price"]) * pos["shares"] * direction
            lines.extend(
                [
                    f"{ticker} {side}",
                    f"  Now: {_format_inr(px)} | Entry: {_format_inr(pos['entry_price'])}",
                    f"  uP&L: {upnl:+,.2f}",
                    f"  Stop: {_format_inr(_position_stop(pos, px))} | Target: {_format_inr(pos.get('take_profit_price'))}",
                ]
            )
    else:
        lines.append("No open positions.")

    _notify("\n".join(lines), quiet=True)


# -- Portfolio -----------------------------------------------------------------

class Portfolio:
    """Tracks cash, open positions, trade log, and daily snapshots."""

    def __init__(self, state_file: str, starting_capital: float) -> None:
        self.state_file = Path(state_file)
        self.starting_capital = starting_capital
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_or_init()

    def _load_or_init(self) -> None:
        if self.state_file.exists():
            with open(self.state_file, encoding="utf-8") as f:
                s = json.load(f)
            self.cash = s["cash"]
            self.positions = s["positions"]
            self.trades = s["trades"]
            self.snapshots = s["snapshots"]
            log.info(
                "Loaded portfolio — cash=₹%.2f  open positions=%d",
                self.cash, len(self.positions),
            )
        else:
            self.cash = self.starting_capital
            self.positions: dict = {}   # ticker → position dict
            self.trades: list = []
            self.snapshots: list = []
            log.info("New portfolio — starting capital ₹%.2f", self.starting_capital)
            self._save()

    def _save(self) -> None:
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "cash": self.cash,
                    "positions": self.positions,
                    "trades": self.trades,
                    "snapshots": self.snapshots,
                },
                f,
                indent=2,
            )

    # -- position mechanics ----------------------------------------------------

    def open_position(
        self, ticker: str, signal: float, price: float, trade_date: str
    ) -> None:
        """Close any existing position then open a new one sized by signal."""
        if ticker in self.positions:
            self._close(ticker, price, trade_date)

        direction = 1 if signal > 0 else -1
        if direction == -1 and not PAPER_CONFIG["allow_short"]:
            log.info("  [%s] Bearish signal — going flat (allow_short=False)", ticker)
            return

        notional = (
            self.starting_capital
            * PAPER_CONFIG["position_size_pct"]
            * abs(signal)
        )

        if direction == 1 and notional > self.cash:
            log.warning(
                "  [%s] Insufficient cash (need ₹%.0f, have ₹%.0f) — capping",
                ticker, notional, self.cash,
            )
            notional = max(0, self.cash * 0.95)

        if notional <= 0:
            log.info("  [%s] Zero notional — skipping", ticker)
            return

        shares = notional / price
        self.cash -= shares * price * direction   # long: cash out; short: cash in

        sl_pct = PAPER_CONFIG["stop_loss_pct"]
        tp_pct = PAPER_CONFIG["take_profit_pct"]
        if direction == 1:   # long
            stop_loss_price   = price * (1 - sl_pct)
            take_profit_price = price * (1 + tp_pct)
        else:                # short
            stop_loss_price   = price * (1 + sl_pct)
            take_profit_price = price * (1 - tp_pct)

        self.positions[ticker] = {
            "shares":           shares,
            "direction":        direction,
            "entry_price":      price,
            "entry_date":       trade_date,
            "signal":           signal,
            "notional":         notional,
            # risk levels — updated on every price check
            "peak_price":       price,           # highest seen (long) / lowest seen (short)
            "stop_loss_price":  stop_loss_price,
            "take_profit_price": take_profit_price,
        }

        action = "BUY" if direction == 1 else "SHORT"
        log.info(
            "  [%s] %-5s  %.4f shares @ ₹%.2f  notional=₹%.2f",
            ticker, action, shares, price, notional,
        )
        log.info(
            "  [%s] Stop=₹%.2f (-%.0f%%)  TrailPct=%.0f%%  TP=₹%.2f (+%.0f%%)",
            ticker,
            stop_loss_price, sl_pct * 100,
            PAPER_CONFIG["trailing_stop_pct"] * 100,
            take_profit_price, tp_pct * 100,
        )
        self.trades.append(
            {
                "date": trade_date,
                "ticker": ticker,
                "action": action,
                "shares": shares,
                "price": price,
                "notional": notional,
                "pnl": None,
            }
        )
        self._save()
        _notify(
            "\n".join(
                [
                    "Trade executed",
                    f"{ticker} {action}",
                    f"Shares: {shares:.4f}",
                    f"Price: {_format_inr(price)}",
                    f"Notional: {_format_inr(notional)}",
                    f"Stop: {_format_inr(stop_loss_price)}",
                    f"Target: {_format_inr(take_profit_price)}",
                ]
            )
        )

    def close_all(self, prices: dict, trade_date: str) -> None:
        for ticker in list(self.positions):
            price = prices.get(ticker, self.positions[ticker]["entry_price"])
            self._close(ticker, price, trade_date)

    def check_and_apply_stops(self, prices: dict, trade_date: str) -> list[str]:
        """
        For every open position, update the trailing peak and fire stop/TP exits.
        Returns the list of tickers that were closed.
        """
        trail_pct = PAPER_CONFIG["trailing_stop_pct"]
        closed: list[str] = []
        peak_updated = False

        for ticker in list(self.positions):
            price = prices.get(ticker)
            if price is None:
                continue

            pos = self.positions[ticker]
            if "stop_loss_price" not in pos:
                continue  # position opened before risk fields were added — skip

            direction = pos["direction"]

            # --- update trailing peak ---
            if direction == 1 and price > pos["peak_price"]:
                pos["peak_price"] = price
                peak_updated = True
            elif direction == -1 and price < pos["peak_price"]:
                pos["peak_price"] = price
                peak_updated = True

            # --- compute effective stop ---
            if direction == 1:
                trailing_stop  = pos["peak_price"] * (1 - trail_pct)
                effective_stop = max(pos["stop_loss_price"], trailing_stop)
                stop_hit = price <= effective_stop
                tp_hit   = price >= pos["take_profit_price"]
            else:
                trailing_stop  = pos["peak_price"] * (1 + trail_pct)
                effective_stop = min(pos["stop_loss_price"], trailing_stop)
                stop_hit = price >= effective_stop
                tp_hit   = price <= pos["take_profit_price"]

            if stop_hit:
                is_trailing = (
                    trailing_stop > pos["stop_loss_price"] if direction == 1
                    else trailing_stop < pos["stop_loss_price"]
                )
                label = "TRAIL_STOP" if is_trailing else "STOP_LOSS"
                log.info(
                    "[%s] %s hit @ ₹%.2f  (stop=₹%.2f  peak=₹%.2f)",
                    ticker, label, price, effective_stop, pos["peak_price"],
                )
                self._close(ticker, price, trade_date, action=label)
                closed.append(ticker)

            elif tp_hit:
                log.info(
                    "[%s] TAKE_PROFIT hit @ ₹%.2f  (target=₹%.2f)",
                    ticker, price, pos["take_profit_price"],
                )
                self._close(ticker, price, trade_date, action="TAKE_PROFIT")
                closed.append(ticker)

        if peak_updated and closed == []:
            # Save updated peak prices even if no position was closed
            self._save()

        return closed

    def _close(self, ticker: str, price: float, trade_date: str,
               action: str | None = None) -> None:
        pos = self.positions.pop(ticker)
        proceeds = pos["shares"] * price * pos["direction"]
        cost = pos["shares"] * pos["entry_price"] * pos["direction"]
        pnl = proceeds - cost
        self.cash += proceeds

        if action is None:
            action = "SELL" if pos["direction"] == 1 else "COVER"
        pct = pnl / abs(cost) * 100 if cost else 0
        log.info(
            "  [%s] %-11s  %.4f shares @ ₹%.2f  P&L=₹%+.2f (%+.2f%%)",
            ticker, action, pos["shares"], price, pnl, pct,
        )
        self.trades.append(
            {
                "date": trade_date,
                "ticker": ticker,
                "action": action,
                "shares": pos["shares"],
                "price": price,
                "notional": proceeds,
                "pnl": round(pnl, 4),
            }
        )
        self._save()
        _notify(
            "\n".join(
                [
                    "Position closed",
                    f"{ticker} {action}",
                    f"Shares: {pos['shares']:.4f}",
                    f"Price: {_format_inr(price)}",
                    f"P&L: {pnl:+,.2f} ({pct:+.2f}%)",
                ]
            )
        )

    # -- valuation -------------------------------------------------------------

    def market_value(self, prices: dict) -> float:
        total = 0.0
        for ticker, pos in self.positions.items():
            px = prices.get(ticker, pos["entry_price"])
            total += pos["shares"] * px * pos["direction"]
        return total

    def total_value(self, prices: dict) -> float:
        return self.cash + self.market_value(prices)

    def position_details(self, prices: dict) -> list[dict]:
        rows = []
        for ticker, pos in self.positions.items():
            px = prices.get(ticker, pos["entry_price"])
            upnl = (px - pos["entry_price"]) * pos["shares"] * pos["direction"]
            rows.append(
                {
                    "ticker": ticker,
                    "side": "LONG" if pos["direction"] == 1 else "SHORT",
                    "shares": pos["shares"],
                    "entry_price": pos["entry_price"],
                    "current_price": px,
                    "unrealised_pnl": upnl,
                    "notional": pos["notional"],
                }
            )
        return rows

    def take_snapshot(self, prices: dict, label: str) -> dict:
        total = self.total_value(prices)
        pnl = total - self.starting_capital
        snap = {
            "ts": datetime.now(IST).isoformat(),
            "label": label,
            "cash": round(self.cash, 2),
            "market_value": round(self.market_value(prices), 2),
            "total_value": round(total, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / self.starting_capital * 100, 4),
        }
        self.snapshots.append(snap)
        self._save()
        return snap

# -- Price helpers -------------------------------------------------------------

def get_prices(tickers: list, mode: str = "last") -> dict:
    """
    Fetch prices for all tickers.
    mode='open'  → today's first 1-min bar open (validates bar is from today;
                   returns nothing for that ticker if market is closed/holiday)
    mode='last'  → fast_info.last_price (live ~15-min delayed or most recent close)
    """
    prices = {}
    today = datetime.now(IST).date()
    import yfinance as yf

    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            if mode == "open":
                hist = t.history(period="1d", interval="1m")
                if not hist.empty:
                    # tz_convert so the index is in IST before extracting date
                    bar_date = hist.index[0].tz_convert(IST).date()
                    if bar_date == today:
                        prices[ticker] = float(hist["Open"].iloc[0])
                    else:
                        log.warning(
                            "Price bar for %s is from %s (stale) — "
                            "NSE may be on holiday or data not yet available",
                            ticker, bar_date,
                        )
                # No valid intraday bar — no fallback; caller detects empty prices
                continue
            # last mode — fine for EOD valuation and position marks
            fi = t.fast_info
            px = fi.last_price or fi.previous_close
            if px:
                prices[ticker] = float(px)
        except Exception as exc:
            log.warning("Price fetch failed for %s: %s", ticker, exc)
    return prices

# -- Market calendar helpers ---------------------------------------------------

def is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5   # Mon–Fri (no holiday calendar; yfinance handles missing data)

def is_friday(dt: datetime) -> bool:
    return dt.weekday() == 4

def time_until(dt: datetime, h: int, m: int) -> float:
    """Seconds until HH:MM today (IST). Negative if already past."""
    target = dt.replace(hour=h, minute=m, second=0, microsecond=0)
    return (target - dt).total_seconds()

# -- Morning analysis ----------------------------------------------------------

def run_morning_analysis(ta, portfolio: Portfolio) -> None:
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    tickers = PAPER_CONFIG["tickers"]

    log.info("-" * 60)
    log.info("MORNING ANALYSIS  %s", today)
    log.info("-" * 60)

    prices = get_prices(tickers, mode="open")
    if not prices:
        log.warning(
            "No valid intraday prices found — NSE is likely on a holiday "
            "or yfinance data is not yet available. Skipping today's analysis."
        )
        _notify(
            "Morning analysis skipped\n"
            "No valid intraday prices found. NSE may be on holiday or yfinance data may be delayed."
        )
        return
    log.info("Execution prices: %s", {t: f"₹{p:.2f}" for t, p in prices.items()})

    for ticker in tickers:
        if ticker not in prices:
            log.warning("[%s] No price — skipping", ticker)
            continue

        price = prices[ticker]
        log.info("[%s] Analysing %s …", ticker, today)

        try:
            from tradingagents.agents.utils.rating import parse_rating

            _, decision = ta.propagate(ticker, today)
            rating = parse_rating(decision)
            signal = RATING_TO_SIGNAL.get(rating)
            log.info("[%s] Rating: %-12s  signal=%s", ticker, rating,
                     f"{signal:+.1f}" if signal is not None else "HOLD")
            _notify(
                "\n".join(
                    [
                        "Morning analysis decision",
                        f"{ticker}: {rating}",
                        f"Signal: {signal:+.1f}" if signal is not None else "Signal: HOLD",
                    ]
                ),
                quiet=True,
            )
        except Exception as exc:
            log.error("[%s] Analysis error: %s", ticker, exc)
            _notify(f"Morning analysis error\n{ticker}: {exc}")
            continue

        # Hold → keep existing position unchanged
        if signal is None:
            log.info("[%s] HOLD — no change to position", ticker)
            continue

        existing = portfolio.positions.get(ticker)
        # Skip if direction and size are identical to what we already hold
        if existing:
            new_dir = 1 if signal > 0 else -1
            if new_dir == existing["direction"] and abs(signal) == abs(existing["signal"]):
                log.info("[%s] Signal unchanged — no trade", ticker)
                continue

        portfolio.open_position(ticker, signal, price, today)

    snap = portfolio.take_snapshot(get_prices(tickers), "morning_open")
    _print_snapshot("MORNING OPEN", snap, portfolio, get_prices(tickers))


# -- Intraday price monitor ----------------------------------------------------

def run_price_check(portfolio: Portfolio) -> None:
    """
    Fetch latest prices for all open positions and apply stop/TP logic.
    Called every price_check_interval seconds during market hours.
    """
    if not portfolio.positions:
        return

    now = datetime.now(IST)
    tickers = list(portfolio.positions.keys())
    prices = get_prices(tickers, mode="last")

    if not prices:
        log.warning("Price check: no prices returned — skipping.")
        return

    trail_pct = PAPER_CONFIG["trailing_stop_pct"]
    log.info("--- price check @ %s ---", now.strftime("%H:%M IST"))
    for ticker, pos in list(portfolio.positions.items()):
        px = prices.get(ticker)
        if px is None or "stop_loss_price" not in pos:
            continue
        direction = pos["direction"]
        peak = pos["peak_price"]
        if direction == 1:
            trailing_stop  = peak * (1 - trail_pct)
            effective_stop = max(pos["stop_loss_price"], trailing_stop)
        else:
            trailing_stop  = peak * (1 + trail_pct)
            effective_stop = min(pos["stop_loss_price"], trailing_stop)
        upnl = (px - pos["entry_price"]) * pos["shares"] * direction
        log.info(
            "  [%s] ₹%.2f  uP&L=₹%+.2f  stop=₹%.2f  TP=₹%.2f  peak=₹%.2f",
            ticker, px, upnl, effective_stop, pos["take_profit_price"], peak,
        )

    today = now.strftime("%Y-%m-%d")
    closed = portfolio.check_and_apply_stops(prices, today)
    if closed:
        log.info("Price check closed %d position(s): %s", len(closed), closed)


# -- EOD summary ---------------------------------------------------------------

def run_eod_close(portfolio: Portfolio) -> None:
    """Day-trading mode: close all open positions at 3:25 PM IST."""
    if not portfolio.positions:
        log.info("EOD close: no open positions.")
        return
    today = datetime.now(IST).strftime("%Y-%m-%d")
    prices = get_prices(PAPER_CONFIG["tickers"])
    log.info("-" * 60)
    log.info("EOD CLOSE (day-trading, NSE)  %s", today)
    log.info("-" * 60)
    portfolio.close_all(prices, today)
    log.info("All positions closed.")
    _notify(f"EOD close complete\nAll open positions closed for {today}.")


def run_eod_summary(portfolio: Portfolio) -> None:
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    tickers = PAPER_CONFIG["tickers"]
    prices = get_prices(tickers)
    snap = portfolio.take_snapshot(prices, f"eod_{today}")
    _print_snapshot("END OF DAY", snap, portfolio, prices)
    _notify(
        "\n".join(
            [
                "End of day summary",
                today,
                f"Total value: {_format_inr(snap['total_value'])}",
                f"Total P&L: {snap['pnl']:+,.2f} ({snap['pnl_pct']:+.2f}%)",
                f"Cash: {_format_inr(snap['cash'])}",
                f"Positions: {_format_inr(snap['market_value'])}",
            ]
        )
    )


def run_eow_summary(portfolio: Portfolio) -> None:
    now = datetime.now(IST)
    week = now.isocalendar()[1]
    week_snaps = [
        s for s in portfolio.snapshots
        if datetime.fromisoformat(s["ts"]).isocalendar()[1] == week
        and s["label"].startswith("morning")
    ]
    if not week_snaps:
        return

    tickers = PAPER_CONFIG["tickers"]
    prices = get_prices(tickers)
    snap = portfolio.take_snapshot(prices, f"eow_{now.strftime('%Y-%m-%d')}")
    week_pnl = snap["total_value"] - week_snaps[0]["total_value"]

    today_ord = now.date().toordinal()
    week_start_ord = today_ord - now.weekday()
    week_trades = [
        t for t in portfolio.trades
        if date.fromisoformat(t["date"]).toordinal() >= week_start_ord
    ]
    closed = [t for t in week_trades if t.get("pnl") is not None]
    wins = sum(1 for t in closed if t["pnl"] > 0)

    print(f"\n{'=' * 55}")
    print(f"  WEEKLY SUMMARY — w/e {now.strftime('%Y-%m-%d')}")
    print(f"{'=' * 55}")
    print(f"  Week P&L:     ₹{week_pnl:>+12,.2f}")
    print(f"  Closed trades:  {len(closed)}   wins={wins}   losses={len(closed)-wins}")
    print(f"  Total P&L:    ₹{snap['pnl']:>+12,.2f}  ({snap['pnl_pct']:+.2f}%)")
    print(f"{'=' * 55}\n")


def _print_snapshot(
    label: str, snap: dict, portfolio: Portfolio, prices: dict
) -> None:
    details = portfolio.position_details(prices)
    print(f"\n{'-' * 55}")
    print(f"  {label}  —  {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}")
    print(f"{'-' * 55}")
    print(f"  Starting capital:  ₹{portfolio.starting_capital:>12,.2f}")
    print(f"  Cash:              ₹{snap['cash']:>12,.2f}")
    print(f"  Positions (mkt):   ₹{snap['market_value']:>12,.2f}")
    print(f"  Total value:       ₹{snap['total_value']:>12,.2f}")
    print(f"  Total P&L:         ₹{snap['pnl']:>+12,.2f}  ({snap['pnl_pct']:+.2f}%)")
    if details:
        print(f"\n  {'Ticker':<14} {'Side':<6} {'Shares':>8}  {'Entry':>9}  {'Now':>9}  {'uP&L':>11}")
        for d in details:
            print(
                f"  {d['ticker']:<14} {d['side']:<6} {d['shares']:>8.3f}"
                f"  ₹{d['entry_price']:>8.2f}  ₹{d['current_price']:>8.2f}"
                f"  ₹{d['unrealised_pnl']:>+10.2f}"
            )
    else:
        print("\n  No open positions.")
    print(f"{'-' * 55}\n")


# -- Main scheduler loop -------------------------------------------------------

def main_loop(ta, portfolio: Portfolio) -> None:
    cfg = PAPER_CONFIG
    ah, am = map(int, cfg["analysis_time"].split(":"))

    day_trading = cfg.get("day_trading", False)
    log.info("Paper trader running (NSE/India). Tickers: %s", cfg["tickers"])
    log.info(
        "Mode: %s | Analysis at %s IST | Ctrl+C to stop.",
        "DAY TRADING (closes 3:25 PM IST)" if day_trading else "SWING (holds overnight)",
        cfg["analysis_time"],
    )

    analysis_done: dict = {}   # date → bool
    close_done: dict = {}      # date → bool (day-trading EOD close)
    eod_done: dict = {}        # date → bool
    if cfg.get("notifications_enabled", True):
        if telegram_configured():
            _notify(
                "Paper trader started\n"
                f"Tickers: {', '.join(cfg['tickers'])}\n"
                f"Status interval: {cfg['notification_interval_seconds'] // 60} min",
                quiet=True,
            )
        else:
            log.warning(
                "Notifications enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are not configured."
            )

    last_price_check: float = 0.0   # monotonic timestamp of last price check
    last_status_notify: float = 0.0

    while True:
        now = datetime.now(IST)
        today_str = now.strftime("%Y-%m-%d")

        if not is_weekday(now):
            log.info("Weekend — sleeping 1 hour.")
            time.sleep(3600)
            continue

        notify_elapsed = time.monotonic() - last_status_notify
        if notify_elapsed >= cfg["notification_interval_seconds"]:
            notify_position_status(portfolio)
            last_status_notify = time.monotonic()

        # Morning analysis
        secs_to_analysis = time_until(now, ah, am)
        if secs_to_analysis > 0 and not analysis_done.get(today_str):
            log.info("NSE market analysis in %.0f min.", secs_to_analysis / 60)
            time.sleep(min(60, secs_to_analysis))
            continue

        if not analysis_done.get(today_str):
            run_morning_analysis(ta, portfolio)
            analysis_done[today_str] = True
            last_price_check = time.monotonic()  # reset after opening trades

        # Intraday price check — every price_check_interval seconds between
        # morning analysis and EOD close (works in both day-trading and swing modes)
        if analysis_done.get(today_str) and not eod_done.get(today_str):
            elapsed = time.monotonic() - last_price_check
            if elapsed >= cfg["price_check_interval"]:
                run_price_check(portfolio)
                last_price_check = time.monotonic()

        # Day-trading: close all positions at 3:25 PM IST (5 min before NSE close)
        if day_trading:
            secs_to_close = time_until(now, 15, 25)
            if secs_to_close > 0:
                time.sleep(min(60, secs_to_close))
                continue
            if not close_done.get(today_str):
                run_eod_close(portfolio)
                close_done[today_str] = True

        # EOD summary at 3:30 PM IST (NSE market close)
        secs_to_eod = time_until(now, 15, 30)
        if secs_to_eod > 0:
            time.sleep(min(60, secs_to_eod))
            continue

        if not eod_done.get(today_str):
            run_eod_summary(portfolio)
            if is_friday(now):
                run_eow_summary(portfolio)
            eod_done[today_str] = True

        # Past EOD — sleep until tomorrow's analysis time
        time.sleep(60)


# -- CLI -----------------------------------------------------------------------

def cmd_status(portfolio: Portfolio) -> None:
    prices = get_prices(PAPER_CONFIG["tickers"])
    snap = {
        "cash": round(portfolio.cash, 2),
        "market_value": round(portfolio.market_value(prices), 2),
        "total_value": round(portfolio.total_value(prices), 2),
        "pnl": round(portfolio.total_value(prices) - portfolio.starting_capital, 2),
        "pnl_pct": round(
            (portfolio.total_value(prices) - portfolio.starting_capital)
            / portfolio.starting_capital * 100, 4
        ),
    }
    _print_snapshot("CURRENT STATUS", snap, portfolio, prices)
    print(f"  Total trades logged: {len(portfolio.trades)}")
    print(f"  State file: {portfolio.state_file.resolve()}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="TradingAgents paper trader")
    parser.add_argument("--status", action="store_true", help="Show portfolio status and exit")
    parser.add_argument("--eod",    action="store_true", help="Print EOD summary now and exit")
    parser.add_argument("--reset",  action="store_true", help="Wipe portfolio back to starting capital")
    args = parser.parse_args()

    cfg = PAPER_CONFIG
    _ensure_runtime_dirs()
    state_file = _runtime_path("state_file")
    log_file = _runtime_path("log_file")
    lock_file = _runtime_path("lock_file")
    _setup_logging(str(log_file))

    if args.reset:
        if state_file.exists():
            state_file.unlink()
        log.info("Portfolio reset to ₹%.2f", cfg["starting_capital"])
        return

    portfolio = Portfolio(str(state_file), cfg["starting_capital"])

    if args.status:
        cmd_status(portfolio)
        return

    if args.eod:
        run_eod_summary(portfolio)
        return

    if not os.getenv("OPENAI_API_KEY"):
        raise EnvironmentError("OPENAI_API_KEY not set — add it to .env or export it.")

    _assert_safe_to_run_bot()
    instance_lock = SingleInstanceLock(lock_file)
    instance_lock.acquire()

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    ta_config = DEFAULT_CONFIG.copy()
    ta_config.update(
        {
            "llm_provider": "openai",
            "deep_think_llm": cfg["deep_think_llm"],
            "quick_think_llm": cfg["quick_think_llm"],
            "max_debate_rounds": cfg["max_debate_rounds"],
            "max_risk_discuss_rounds": cfg["max_risk_discuss_rounds"],
            "data_vendors": {
                "core_stock_apis":      "yfinance",
                "technical_indicators": "yfinance",
                "fundamental_data":     "yfinance",
                "news_data":            "rss",       # ET RSS feeds (India)
            },
        }
    )
    ta = TradingAgentsGraph(debug=False, config=ta_config)

    try:
        main_loop(ta, portfolio)
    except KeyboardInterrupt:
        log.info("Stopped. Portfolio state saved to %s", portfolio.state_file)
    finally:
        instance_lock.release()


if __name__ == "__main__":
    main()
