#!/usr/bin/env python3
"""
backtest.py — TradingAgents US market backtester (OpenAI)

Usage:
    python backtest.py

Edit BACKTEST_CONFIG below to set tickers, date range, models, and frequency.
Results are written incrementally to backtest_results.csv so a crashed run
can be resumed without re-running completed trades.

Cost estimate (weekly frequency, gpt-4o):
    ~$0.50–2.00 per ticker per month of history.
    Start with a short date range (e.g. one month, 2–3 tickers) to validate.
"""

import csv
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from tradingagents.agents.utils.rating import parse_rating
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

load_dotenv()

# ── Backtest configuration ────────────────────────────────────────────────────
# Edit these values before running.

BACKTEST_CONFIG = {
    # US stock tickers to backtest
    "tickers": ["AAPL", "MSFT", "NVDA", "GOOGL"],

    # Inclusive date range (YYYY-MM-DD). Dates must be in the past so that
    # yfinance can supply the actual price data needed to compute returns.
    "start_date": "2026-03-01",
    "end_date": "2026-03-31",

    # Analysis frequency:
    #   "weekly"  → first US business day of each calendar week  (recommended)
    #   "monthly" → first US business day of each calendar month (cheapest)
    #   "daily"   → every US business day                        (expensive)
    "frequency": "weekly",

    # Number of trading days to hold each position when computing the return.
    "holding_days": 5,

    # OpenAI models — change to "gpt-4o" / "gpt-4o-mini" if gpt-5.4 is unavailable.
    "deep_think_llm": "gpt-4o",
    "quick_think_llm": "gpt-4o-mini",

    # LLM debate depth. 1 = fastest/cheapest, 3 = most thorough.
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,

    # Output CSV path (relative to working directory)
    "output_csv": "backtest_results.csv",

    # Seconds to pause between runs to avoid OpenAI rate-limit bursts.
    "sleep_between_runs": 5,
}

# ── Rating → signed position size ─────────────────────────────────────────────
# Buy=full long (+1), Overweight=half long (+0.5), Hold=flat (0),
# Underweight=half short (−0.5), Sell=full short (−1).
RATING_TO_POSITION = {
    "Buy":         1.0,
    "Overweight":  0.5,
    "Hold":        0.0,
    "Underweight": -0.5,
    "Sell":        -1.0,
}

_CSV_FIELDS = [
    "ticker", "date", "rating", "position",
    "raw_return", "alpha_return", "holding_days",
    "pnl",   # position * raw_return (sign-corrected P&L proxy)
    "error",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Date helpers ──────────────────────────────────────────────────────────────

def generate_trading_dates(start: str, end: str, frequency: str) -> list[str]:
    """Return US business-day dates in [start, end] at the requested frequency."""
    bdays = pd.bdate_range(start=start, end=end, freq="B")

    if frequency == "daily":
        return [d.strftime("%Y-%m-%d") for d in bdays]

    if frequency == "weekly":
        seen, dates = set(), []
        for d in bdays:
            key = (d.year, d.isocalendar()[1])
            if key not in seen:
                seen.add(key)
                dates.append(d.strftime("%Y-%m-%d"))
        return dates

    if frequency == "monthly":
        seen, dates = set(), []
        for d in bdays:
            key = (d.year, d.month)
            if key not in seen:
                seen.add(key)
                dates.append(d.strftime("%Y-%m-%d"))
        return dates

    raise ValueError(f"Unknown frequency {frequency!r}. Use 'daily', 'weekly', or 'monthly'.")


# ── CSV helpers ───────────────────────────────────────────────────────────────

def load_completed_runs(csv_path: str) -> set[tuple[str, str]]:
    """Return (ticker, date) pairs already present in the output CSV."""
    done: set[tuple[str, str]] = set()
    if not Path(csv_path).exists():
        return done
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            done.add((row["ticker"], row["date"]))
    return done


def append_row(csv_path: str, row: dict, write_header: bool) -> None:
    mode = "w" if write_header else "a"
    with open(csv_path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in _CSV_FIELDS})


# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_metrics(df: pd.DataFrame) -> dict:
    df = df[df["error"] == ""].copy()
    for col in ("pnl", "position", "raw_return", "alpha_return"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    active = df[df["position"] != 0].dropna(subset=["pnl"])
    if active.empty:
        return {"note": "No active (non-Hold) trades with resolved returns yet."}

    n = len(active)
    win_rate = (active["pnl"] > 0).mean()
    total_pnl = active["pnl"].sum()
    avg_alpha = active["alpha_return"].mean()

    mean_p, std_p = active["pnl"].mean(), active["pnl"].std()
    periods_per_year = {"daily": 252, "weekly": 52, "monthly": 12}.get(
        BACKTEST_CONFIG["frequency"], 52
    )
    sharpe = (mean_p / std_p) * (periods_per_year ** 0.5) if std_p and std_p > 0 else float("nan")

    cum = active["pnl"].cumsum()
    max_dd = (cum - cum.cummax()).min()

    rating_counts = active["rating"].value_counts().to_dict()

    return {
        "total_trades":          n,
        "win_rate":              round(win_rate, 4),
        "total_pnl":             round(total_pnl, 4),
        "avg_alpha_per_trade":   round(avg_alpha, 4),
        "annualised_sharpe":     round(sharpe, 4),
        "max_drawdown":          round(max_dd, 4),
        "rating_distribution":   rating_counts,
    }


def print_summary(csv_path: str) -> None:
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        log.error("Cannot read results CSV: %s", e)
        return

    metrics = compute_metrics(df)
    print("\n=== BACKTEST SUMMARY ===")
    for k, v in metrics.items():
        print(f"  {k:<28} {v}")

    df_ok = df[df["error"] == ""].copy()
    for col in ("pnl", "position"):
        df_ok[col] = pd.to_numeric(df_ok[col], errors="coerce")
    active = df_ok[df_ok["position"] != 0].dropna(subset=["pnl"])
    if not active.empty:
        print("\n=== PER-TICKER BREAKDOWN ===")
        for ticker, grp in active.groupby("ticker"):
            wins = (grp["pnl"] > 0).sum()
            print(
                f"  {ticker:<8}  trades={len(grp):<4}  wins={wins:<4}  "
                f"total_pnl={grp['pnl'].sum():+.4f}  "
                f"avg_alpha={grp['alpha_return'].mean():+.4f}"
            )

    errors = df[df["error"] != ""]
    if not errors.empty:
        print(f"\n  {len(errors)} failed run(s) — see 'error' column in {csv_path}")


# ── Main backtest loop ────────────────────────────────────────────────────────

def run_backtest() -> None:
    cfg = BACKTEST_CONFIG

    if not os.getenv("OPENAI_API_KEY"):
        raise EnvironmentError(
            "OPENAI_API_KEY not set. Export it or put it in a .env file."
        )

    # Warn if end_date is in the future — returns can't be resolved yet.
    end_dt = datetime.strptime(cfg["end_date"], "%Y-%m-%d")
    if end_dt > datetime.now():
        log.warning(
            "end_date %s is in the future. Returns for recent dates will be "
            "empty until enough time passes for yfinance to supply price data.",
            cfg["end_date"],
        )

    ta_config = DEFAULT_CONFIG.copy()
    ta_config.update(
        {
            "llm_provider": "openai",
            "deep_think_llm": cfg["deep_think_llm"],
            "quick_think_llm": cfg["quick_think_llm"],
            "max_debate_rounds": cfg["max_debate_rounds"],
            "max_risk_discuss_rounds": cfg["max_risk_discuss_rounds"],
            "data_vendors": {
                "core_stock_apis": "yfinance",
                "technical_indicators": "yfinance",
                "fundamental_data": "yfinance",
                "news_data": "yfinance",
            },
        }
    )

    ta = TradingAgentsGraph(debug=False, config=ta_config)

    dates = generate_trading_dates(cfg["start_date"], cfg["end_date"], cfg["frequency"])
    tickers = cfg["tickers"]
    total = len(tickers) * len(dates)

    log.info(
        "Backtest: %d tickers × %d dates = %d runs  [%s → %s, %s]",
        len(tickers), len(dates), total,
        cfg["start_date"], cfg["end_date"], cfg["frequency"],
    )

    csv_path = cfg["output_csv"]
    done = load_completed_runs(csv_path)
    needs_header = not Path(csv_path).exists()

    n = 0
    for ticker in tickers:
        for date in dates:
            n += 1
            if (ticker, date) in done:
                log.info("[%d/%d] SKIP  %s %s", n, total, ticker, date)
                continue

            log.info("[%d/%d] RUN   %s %s", n, total, ticker, date)
            row: dict = {"ticker": ticker, "date": date, "error": ""}

            try:
                _, decision = ta.propagate(ticker, date)
                rating = parse_rating(decision)
                position = RATING_TO_POSITION.get(rating, 0.0)

                raw, alpha, hold_days = ta._fetch_returns(
                    ticker, date, cfg["holding_days"]
                )

                row["rating"] = rating
                row["position"] = position
                row["raw_return"] = f"{raw:.6f}" if raw is not None else ""
                row["alpha_return"] = f"{alpha:.6f}" if alpha is not None else ""
                row["holding_days"] = hold_days if hold_days is not None else ""
                row["pnl"] = f"{position * raw:.6f}" if raw is not None else ""

                log.info(
                    "  → %-12s  raw=%+.2f%%  alpha=%+.2f%%",
                    rating,
                    (raw * 100 if raw else 0),
                    (alpha * 100 if alpha else 0),
                )

            except Exception as exc:
                log.error("  ERROR: %s", exc)
                row["error"] = str(exc)[:300]

            append_row(csv_path, row, write_header=needs_header)
            needs_header = False

            if cfg["sleep_between_runs"] > 0 and n < total:
                time.sleep(cfg["sleep_between_runs"])

    log.info("All runs complete.")
    print_summary(csv_path)
    print(f"\nFull results → {Path(csv_path).resolve()}")


if __name__ == "__main__":
    run_backtest()
