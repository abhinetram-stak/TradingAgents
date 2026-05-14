#!/usr/bin/env python3
"""
paper_trader.py — TradingAgents paper trading engine

Runs continuously during US market hours. At the configured analysis time
(default 9:35 AM ET) it runs TradingAgents on each ticker, sizes positions
based on the 5-tier rating, and logs all paper orders. At 4:00 PM ET it prints
an EOD P&L summary. On Fridays it also prints a weekly summary.

State (positions, trades, P&L history) is persisted to paper_portfolio.json
so the process can be restarted without losing data.

Usage:
    uv run python paper_trader.py              # run live (leave it running)
    uv run python paper_trader.py --status     # show current portfolio & exit
    uv run python paper_trader.py --eod        # print EOD summary now & exit
    uv run python paper_trader.py --reset      # wipe portfolio back to starting capital
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf
from dotenv import load_dotenv

from tradingagents.agents.utils.rating import parse_rating
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

load_dotenv()

ET = ZoneInfo("America/New_York")

# -- Configuration -------------------------------------------------------------

PAPER_CONFIG = {
    # Tickers to trade
    "tickers": ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"],

    # Notional starting capital in USD
    "starting_capital": 100_000.0,

    # Capital allocated per ticker as a fraction of starting_capital.
    # 5 tickers × 0.18 = 90% deployed, 10% cash buffer.
    "position_size_pct": 0.18,

    # If False, Underweight/Sell ratings close any long but don't go short.
    # Set to True to allow short positions.
    "allow_short": False,

    # Day-trading mode: close ALL open positions at 3:55 PM ET every day.
    # False = swing mode (positions carry overnight until signal changes).
    "day_trading": True,

    # Intraday price guards (checked every monitor_interval_secs).
    # Set any to None to disable that guard.
    "stop_loss_pct":     0.02,   # close if position loses 2% from entry (fixed floor)
    "take_profit_pct":   0.03,   # close if position gains 3% from entry (hard ceiling)
    "trailing_stop_pct": 0.02,   # close if price drops 2% from its intraday peak
    "monitor_interval_secs": 300,   # price check frequency (5 min)

    # Time to run morning analysis (ET). 9:35 lets the open settle.
    "analysis_time": "09:35",

    # OpenAI models
    "deep_think_llm": "gpt-4o",
    "quick_think_llm": "gpt-4o-mini",
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,

    # Persistence paths
    "state_file": "paper_portfolio.json",
    "log_file":   "paper_trades.log",
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
    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

log = logging.getLogger(__name__)

# -- Portfolio -----------------------------------------------------------------

class Portfolio:
    """Tracks cash, open positions, trade log, and daily snapshots."""

    def __init__(self, state_file: str, starting_capital: float) -> None:
        self.state_file = Path(state_file)
        self.starting_capital = starting_capital
        self._load_or_init()

    def _load_or_init(self) -> None:
        if self.state_file.exists():
            with open(self.state_file, encoding="utf-8") as f:
                s = json.load(f)
            self.cash = s["cash"]
            self.positions = s["positions"]
            self.trades = s["trades"]
            self.snapshots = s["snapshots"]
            # Back-fill peak_price for positions saved before trailing stop was added
            for pos in self.positions.values():
                pos.setdefault("peak_price", pos["entry_price"])
            log.info(
                "Loaded portfolio — cash=$%.2f  open positions=%d",
                self.cash, len(self.positions),
            )
        else:
            self.cash = self.starting_capital
            self.positions: dict = {}   # ticker → position dict
            self.trades: list = []
            self.snapshots: list = []
            log.info("New portfolio — starting capital $%.2f", self.starting_capital)
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
                "  [%s] Insufficient cash (need $%.0f, have $%.0f) — capping",
                ticker, notional, self.cash,
            )
            notional = max(0, self.cash * 0.95)

        if notional <= 0:
            log.info("  [%s] Zero notional — skipping", ticker)
            return

        shares = notional / price
        self.cash -= shares * price * direction   # long: cash out; short: cash in

        self.positions[ticker] = {
            "shares": shares,
            "direction": direction,
            "entry_price": price,
            "peak_price": price,   # updated each monitor tick; drives the trailing stop
            "entry_date": trade_date,
            "signal": signal,
            "notional": notional,
        }

        action = "BUY" if direction == 1 else "SHORT"
        log.info(
            "  [%s] %-5s  %.4f shares @ $%.2f  notional=$%.2f",
            ticker, action, shares, price, notional,
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

    def close_all(self, prices: dict, trade_date: str, reason: str = "EOD") -> None:
        for ticker in list(self.positions):
            price = prices.get(ticker, self.positions[ticker]["entry_price"])
            self._close(ticker, price, trade_date, reason=reason)

    def _close(self, ticker: str, price: float, trade_date: str, reason: str = "SIGNAL") -> None:
        pos = self.positions.pop(ticker)
        proceeds = pos["shares"] * price * pos["direction"]
        cost = pos["shares"] * pos["entry_price"] * pos["direction"]
        pnl = proceeds - cost
        self.cash += proceeds

        action = "SELL" if pos["direction"] == 1 else "COVER"
        pct = pnl / abs(cost) * 100 if cost else 0
        log.info(
            "  [%s] %-5s  %.4f shares @ $%.2f  P&L=$%+.2f (%+.2f%%)  reason=%s",
            ticker, action, pos["shares"], price, pnl, pct, reason,
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
                "reason": reason,
            }
        )
        self._save()

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
            "ts": datetime.now(ET).isoformat(),
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
    mode='open'  → today's first 1-min bar open
    mode='last'  → fast_info.last_price (live or most recent close)
    """
    prices = {}
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            if mode == "open":
                hist = t.history(period="1d", interval="1m")
                if not hist.empty:
                    prices[ticker] = float(hist["Open"].iloc[0])
                    continue
            # fallback / last mode
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
    """Seconds until HH:MM today (ET). Negative if already past."""
    target = dt.replace(hour=h, minute=m, second=0, microsecond=0)
    return (target - dt).total_seconds()

# -- Morning analysis ----------------------------------------------------------

def run_morning_analysis(ta: TradingAgentsGraph, portfolio: Portfolio) -> None:
    now = datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    tickers = PAPER_CONFIG["tickers"]

    log.info("-" * 60)
    log.info("MORNING ANALYSIS  %s", today)
    log.info("-" * 60)

    prices = get_prices(tickers, mode="open")
    if not prices:
        log.error("Could not fetch any prices — is the market open?")
        return
    log.info("Execution prices: %s", {t: f"${p:.2f}" for t, p in prices.items()})

    for ticker in tickers:
        if ticker not in prices:
            log.warning("[%s] No price — skipping", ticker)
            continue

        price = prices[ticker]
        log.info("[%s] Analysing %s …", ticker, today)

        try:
            _, decision = ta.propagate(ticker, today)
            rating = parse_rating(decision)
            signal = RATING_TO_SIGNAL.get(rating)
            log.info("[%s] Rating: %-12s  signal=%s", ticker, rating,
                     f"{signal:+.1f}" if signal is not None else "HOLD")
        except Exception as exc:
            log.error("[%s] Analysis error: %s", ticker, exc)
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

def run_intraday_monitor(portfolio: Portfolio) -> None:
    """Update peak prices and check every open position against price guards.

    Guard priority (first match wins):
      1. Fixed stop-loss  — protects capital early before the trail has moved up
      2. Trailing stop    — locks in gains as price moves favorably
      3. Take-profit      — hard ceiling regardless of trail
    """
    if not portfolio.positions:
        return

    sl  = PAPER_CONFIG.get("stop_loss_pct")
    ts  = PAPER_CONFIG.get("trailing_stop_pct")
    tp  = PAPER_CONFIG.get("take_profit_pct")
    if sl is None and ts is None and tp is None:
        return

    today = datetime.now(ET).strftime("%Y-%m-%d")
    tickers = list(portfolio.positions.keys())
    prices = get_prices(tickers)
    peak_updated = False

    for ticker in tickers:
        pos = portfolio.positions.get(ticker)
        if pos is None:
            continue   # closed earlier in this loop

        current = prices.get(ticker)
        if current is None:
            continue

        direction = pos["direction"]
        entry     = pos["entry_price"]
        peak      = pos.get("peak_price", entry)

        # --- update peak (most favourable price seen so far) ---
        # long: peak is the highest price; short: peak is the lowest price
        new_peak = max(peak, current) if direction == 1 else min(peak, current)
        if new_peak != peak:
            pos["peak_price"] = new_peak
            peak = new_peak
            peak_updated = True

        # pnl_pct > 0  →  position is making money
        pnl_pct = (current - entry) / entry * direction

        # 1. Fixed stop-loss (from entry)
        if sl is not None and pnl_pct <= -sl:
            log.warning(
                "  [%s] STOP-LOSS       entry=$%.2f  now=$%.2f  pnl=%.2f%%  (limit=-%.0f%%)",
                ticker, entry, current, pnl_pct * 100, sl * 100,
            )
            portfolio._close(ticker, current, today, reason="STOP-LOSS")
            continue

        # 2. Trailing stop (from peak)
        if ts is not None:
            # how far has price retreated from the peak, in the unfavourable direction?
            trail_drawdown = (peak - current) / peak * direction
            trail_level = peak * (1 - ts) if direction == 1 else peak * (1 + ts)
            if trail_drawdown >= ts:
                log.warning(
                    "  [%s] TRAILING-STOP   peak=$%.2f  now=$%.2f  trail_level=$%.2f  pnl=%.2f%%",
                    ticker, peak, current, trail_level, pnl_pct * 100,
                )
                portfolio._close(ticker, current, today, reason="TRAILING-STOP")
                continue

        # 3. Take-profit (hard ceiling)
        if tp is not None and pnl_pct >= tp:
            log.info(
                "  [%s] TAKE-PROFIT      entry=$%.2f  now=$%.2f  gain=%.2f%%  (target=+%.0f%%)",
                ticker, entry, current, pnl_pct * 100, tp * 100,
            )
            portfolio._close(ticker, current, today, reason="TAKE-PROFIT")
            continue

    if peak_updated:
        portfolio._save()


# -- EOD summary ---------------------------------------------------------------

def run_eod_close(portfolio: Portfolio) -> None:
    """Day-trading mode: close all open positions at 3:55 PM ET."""
    if not portfolio.positions:
        log.info("EOD close: no open positions.")
        return
    today = datetime.now(ET).strftime("%Y-%m-%d")
    prices = get_prices(PAPER_CONFIG["tickers"])
    log.info("-" * 60)
    log.info("EOD CLOSE (day-trading)  %s", today)
    log.info("-" * 60)
    portfolio.close_all(prices, today)
    log.info("All positions closed.")


def run_eod_summary(portfolio: Portfolio) -> None:
    now = datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    tickers = PAPER_CONFIG["tickers"]
    prices = get_prices(tickers)
    snap = portfolio.take_snapshot(prices, f"eod_{today}")
    _print_snapshot("END OF DAY", snap, portfolio, prices)


def run_eow_summary(portfolio: Portfolio) -> None:
    now = datetime.now(ET)
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
    print(f"  Week P&L:     ${week_pnl:>+12,.2f}")
    print(f"  Closed trades:  {len(closed)}   wins={wins}   losses={len(closed)-wins}")
    print(f"  Total P&L:    ${snap['pnl']:>+12,.2f}  ({snap['pnl_pct']:+.2f}%)")
    print(f"{'=' * 55}\n")


def _print_snapshot(
    label: str, snap: dict, portfolio: Portfolio, prices: dict
) -> None:
    details = portfolio.position_details(prices)
    print(f"\n{'-' * 55}")
    print(f"  {label}  —  {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}")
    print(f"{'-' * 55}")
    print(f"  Starting capital:  ${portfolio.starting_capital:>12,.2f}")
    print(f"  Cash:              ${snap['cash']:>12,.2f}")
    print(f"  Positions (mkt):   ${snap['market_value']:>12,.2f}")
    print(f"  Total value:       ${snap['total_value']:>12,.2f}")
    print(f"  Total P&L:         ${snap['pnl']:>+12,.2f}  ({snap['pnl_pct']:+.2f}%)")
    if details:
        print(f"\n  {'Ticker':<8} {'Side':<6} {'Shares':>8}  {'Entry':>8}  {'Now':>8}  {'uP&L':>10}")
        for d in details:
            print(
                f"  {d['ticker']:<8} {d['side']:<6} {d['shares']:>8.3f}"
                f"  ${d['entry_price']:>7.2f}  ${d['current_price']:>7.2f}"
                f"  ${d['unrealised_pnl']:>+9.2f}"
            )
    else:
        print("\n  No open positions.")
    print(f"{'-' * 55}\n")


# -- Main scheduler loop -------------------------------------------------------

def main_loop(ta: TradingAgentsGraph, portfolio: Portfolio) -> None:
    cfg = PAPER_CONFIG
    ah, am = map(int, cfg["analysis_time"].split(":"))

    day_trading = cfg.get("day_trading", False)
    monitor_secs = cfg.get("monitor_interval_secs", 300)
    sl = cfg.get("stop_loss_pct")
    tp = cfg.get("take_profit_pct")

    log.info("Paper trader running. Tickers: %s", cfg["tickers"])
    log.info(
        "Mode: %s | Analysis at %s ET | Ctrl+C to stop.",
        "DAY TRADING (closes 3:55 PM)" if day_trading else "SWING (holds overnight)",
        cfg["analysis_time"],
    )
    if sl or tp:
        log.info(
            "Price guards active — stop-loss: %s  take-profit: %s  check every %ds",
            f"{sl*100:.0f}%" if sl else "off",
            f"{tp*100:.0f}%" if tp else "off",
            monitor_secs,
        )

    analysis_done: dict = {}   # date → bool
    close_done: dict = {}      # date → bool (day-trading EOD close)
    eod_done: dict = {}        # date → bool

    while True:
        now = datetime.now(ET)
        today_str = now.strftime("%Y-%m-%d")

        if not is_weekday(now):
            log.info("Weekend — sleeping 1 hour.")
            time.sleep(3600)
            continue

        # Morning analysis
        secs_to_analysis = time_until(now, ah, am)
        if secs_to_analysis > 0 and not analysis_done.get(today_str):
            log.info("Market analysis in %.0f min.", secs_to_analysis / 60)
            time.sleep(min(60, secs_to_analysis))
            continue

        if not analysis_done.get(today_str):
            run_morning_analysis(ta, portfolio)
            analysis_done[today_str] = True

        # Between open and 3:55 PM: run the price monitor on each wake-up
        if day_trading and not close_done.get(today_str):
            secs_to_close = time_until(now, 15, 55)
            if secs_to_close > 0:
                if portfolio.positions:
                    run_intraday_monitor(portfolio)
                time.sleep(min(monitor_secs, secs_to_close))
                continue
            # 3:55 PM reached — force-close anything the monitor didn't catch
            run_eod_close(portfolio)
            close_done[today_str] = True

        # Swing mode: run monitor between open and 4 PM
        if not day_trading and (sl or tp) and not eod_done.get(today_str):
            secs_to_eod = time_until(now, 16, 0)
            if secs_to_eod > 0:
                if portfolio.positions:
                    run_intraday_monitor(portfolio)
                time.sleep(min(monitor_secs, secs_to_eod))
                continue

        # EOD summary at 4:00 PM ET
        secs_to_eod = time_until(now, 16, 0)
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
    _setup_logging(cfg["log_file"])

    if args.reset:
        p = Path(cfg["state_file"])
        if p.exists():
            p.unlink()
        log.info("Portfolio reset to $%.2f", cfg["starting_capital"])
        return

    portfolio = Portfolio(cfg["state_file"], cfg["starting_capital"])

    if args.status:
        cmd_status(portfolio)
        return

    if args.eod:
        run_eod_summary(portfolio)
        return

    if not os.getenv("OPENAI_API_KEY"):
        raise EnvironmentError("OPENAI_API_KEY not set — add it to .env or export it.")

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
                "news_data":            "yfinance",
            },
        }
    )
    ta = TradingAgentsGraph(debug=False, config=ta_config)

    try:
        main_loop(ta, portfolio)
    except KeyboardInterrupt:
        log.info("Stopped. Portfolio state saved to %s", portfolio.state_file)


if __name__ == "__main__":
    main()
