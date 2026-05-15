#!/usr/bin/env python3
"""Local web dashboard for the TradingAgents paper trader."""

from __future__ import annotations

import contextlib
import base64
import hmac
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

import paper_trader
from paper_trader import IST, PAPER_CONFIG, Portfolio

load_dotenv()

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "web"
CONFIG_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()
BOT_PROCESS: subprocess.Popen | None = None


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        if not path.exists():
            return fallback
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _state_file() -> Path:
    path = Path(PAPER_CONFIG["state_file"]).expanduser()
    if path.is_absolute():
        return path
    return Path(PAPER_CONFIG["data_dir"]).expanduser() / path


def _log_file() -> Path:
    path = Path(PAPER_CONFIG["log_file"]).expanduser()
    if path.is_absolute():
        return path
    return Path(PAPER_CONFIG["data_dir"]).expanduser() / path


def _process_log_file() -> Path:
    return _log_file().with_name("paper_trader.process.log")


def _lock_file() -> Path:
    path = Path(PAPER_CONFIG["lock_file"]).expanduser()
    if path.is_absolute():
        return path
    return Path(PAPER_CONFIG["data_dir"]).expanduser() / path


def _portfolio() -> Portfolio:
    return Portfolio(str(_state_file()), PAPER_CONFIG["starting_capital"])


def _latest_logged_marks() -> dict[str, float]:
    """Read latest position marks from paper_trades.log price-check lines."""
    marks: dict[str, float] = {}
    pattern = re.compile(
        r"\[(?P<ticker>[A-Z0-9_.-]+)\]\s+[^0-9-]*(?P<price>[0-9][0-9,]*\.?[0-9]*)\s+.*?uP&L=",
        re.IGNORECASE,
    )
    for line in _tail_lines(_log_file(), 700):
        match = pattern.search(line)
        if not match:
            continue
        try:
            marks[match.group("ticker")] = float(match.group("price").replace(",", ""))
        except ValueError:
            continue
    return marks


def _prices_for_status(state: dict[str, Any]) -> dict[str, float]:
    prices = _latest_logged_marks()
    if os.getenv("DASHBOARD_LIVE_PRICES", "0").lower() not in {"1", "true", "yes"}:
        return prices
    tickers = list(PAPER_CONFIG["tickers"])
    tickers.extend(t for t in state.get("positions", {}) if t not in tickers)
    try:
        live_prices = paper_trader.get_prices(tickers, mode="last")
        prices.update(live_prices)
    except Exception:
        pass
    return prices


def _portfolio_summary(state: dict[str, Any]) -> dict[str, Any]:
    cash = float(state.get("cash", PAPER_CONFIG["starting_capital"]))
    prices = _prices_for_status(state)
    positions = []
    market_value = 0.0
    unrealised_pnl = 0.0

    for ticker, pos in state.get("positions", {}).items():
        entry = float(pos.get("entry_price", 0) or 0)
        shares = float(pos.get("shares", 0) or 0)
        direction = int(pos.get("direction", 1) or 1)
        price = float(prices.get(ticker, entry) or entry)
        value = shares * price * direction
        pnl = (price - entry) * shares * direction
        market_value += value
        unrealised_pnl += pnl
        trailing_pct = float(PAPER_CONFIG.get("trailing_stop_pct", 0) or 0)
        peak = float(pos.get("peak_price", entry) or entry)
        if direction == 1:
            trailing_stop = peak * (1 - trailing_pct)
            effective_stop = max(float(pos.get("stop_loss_price", 0) or 0), trailing_stop)
        else:
            trailing_stop = peak * (1 + trailing_pct)
            stop_price = float(pos.get("stop_loss_price", 0) or trailing_stop)
            effective_stop = min(stop_price, trailing_stop)
        positions.append(
            {
                "ticker": ticker,
                "side": "LONG" if direction == 1 else "SHORT",
                "shares": shares,
                "entry_price": entry,
                "current_price": price,
                "notional": float(pos.get("notional", 0) or 0),
                "signal": pos.get("signal"),
                "entry_date": pos.get("entry_date"),
                "peak_price": peak,
                "stop_loss_price": float(pos.get("stop_loss_price", 0) or 0),
                "effective_stop_price": effective_stop,
                "take_profit_price": float(pos.get("take_profit_price", 0) or 0),
                "unrealised_pnl": pnl,
                "market_value": value,
            }
        )

    total_value = cash + market_value
    starting = float(PAPER_CONFIG["starting_capital"])
    return {
        "cash": cash,
        "market_value": market_value,
        "total_value": total_value,
        "starting_capital": starting,
        "pnl": total_value - starting,
        "pnl_pct": ((total_value - starting) / starting * 100) if starting else 0,
        "unrealised_pnl": unrealised_pnl,
        "positions": positions,
        "prices": prices,
    }


def _tail_lines(path: Path, limit: int) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-limit:]
    except Exception as exc:
        return [f"Could not read log: {exc}"]


def _analysis_log_root() -> Path:
    configured = Path(
        os.getenv(
            "TRADINGAGENTS_RESULTS_DIR",
            Path.home() / ".tradingagents" / "logs",
        )
    ).expanduser()
    return configured if configured.is_absolute() else ROOT / configured


def _state_log_files() -> list[Path]:
    root = _analysis_log_root()
    if not root.exists():
        return []
    return sorted(
        root.glob("*/TradingAgentsStrategy_logs/full_states_log_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _clip(value: Any, limit: int = 12000) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n[trimmed for dashboard display]"


def _clip_mapping(data: dict[str, Any], limit: int = 12000) -> dict[str, str]:
    return {key: _clip(value, limit) for key, value in data.items()}


def _compact_state(path: Path) -> dict[str, Any]:
    data = _read_json(path, {})
    rel = str(path)
    with contextlib.suppress(ValueError):
        rel = str(path.relative_to(_analysis_log_root()))
    return {
        "path": str(path),
        "relative_path": rel,
        "ticker": path.parents[1].name if len(path.parents) >= 2 else data.get("company_of_interest"),
        "trade_date": data.get("trade_date"),
        "company": data.get("company_of_interest"),
        "modified": datetime.fromtimestamp(path.stat().st_mtime, IST).isoformat(),
        "final_trade_decision": _clip(data.get("final_trade_decision", "")),
        "investment_plan": _clip(data.get("investment_plan", "")),
        "trader_investment_decision": _clip(data.get("trader_investment_decision", "")),
        "investment_debate_state": _clip_mapping(data.get("investment_debate_state", {})),
        "risk_debate_state": _clip_mapping(data.get("risk_debate_state", {})),
        "reports": {
            "market": _clip(data.get("market_report", "")),
            "sentiment": _clip(data.get("sentiment_report", "")),
            "news": _clip(data.get("news_report", "")),
            "fundamentals": _clip(data.get("fundamentals_report", "")),
        },
    }


def _status_payload() -> dict[str, Any]:
    state = _read_json(_state_file(), {"cash": PAPER_CONFIG["starting_capital"], "positions": {}, "trades": [], "snapshots": []})
    return {
        "now": datetime.now(IST).isoformat(),
        "config": PAPER_CONFIG,
        "files": {
            "state_file": str(_state_file()),
            "log_file": str(_log_file()),
            "analysis_log_root": str(_analysis_log_root()),
        },
        "portfolio": _portfolio_summary(state),
        "trades": state.get("trades", []),
        "snapshots": state.get("snapshots", []),
        "bot": _bot_status(),
        "safety": {
            "auth_required": _auth_required(),
            "mutating_controls_enabled": _mutating_controls_enabled(),
            "lock_file": str(_lock_file()),
        },
        "latest_states": [_compact_state(path) for path in _state_log_files()[:8]],
    }


def _mutating_controls_enabled() -> bool:
    return PAPER_CONFIG["bot_enabled"] and PAPER_CONFIG["trading_mode"] == "paper"


def _auth_required() -> bool:
    return bool(os.getenv("DASHBOARD_PASSWORD") or os.getenv("DASHBOARD_TOKEN"))


def _authorized(headers) -> bool:
    password = os.getenv("DASHBOARD_PASSWORD")
    token = os.getenv("DASHBOARD_TOKEN")
    auth = headers.get("Authorization", "")

    if token and auth.startswith("Bearer "):
        return hmac.compare_digest(auth.removeprefix("Bearer ").strip(), token)

    if password and auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth.removeprefix("Basic ").strip()).decode("utf-8")
        except Exception:
            return False
        username, sep, supplied = decoded.partition(":")
        return bool(sep) and username == "admin" and hmac.compare_digest(supplied, password)

    return not _auth_required()


def _capture_output(func, *args) -> dict[str, Any]:
    buffer = io.StringIO()
    with RUN_LOCK:
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                result = func(*args)
            return {"ok": True, "output": buffer.getvalue(), "result": result}
        except Exception as exc:
            return {"ok": False, "output": buffer.getvalue(), "error": str(exc), "traceback": traceback.format_exc()}


def _make_trading_graph():
    if not os.getenv("OPENAI_API_KEY"):
        raise EnvironmentError("OPENAI_API_KEY is not set. Add it to .env before running analysis.")
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    cfg = DEFAULT_CONFIG.copy()
    cfg.update(
        {
            "llm_provider": "openai",
            "deep_think_llm": PAPER_CONFIG["deep_think_llm"],
            "quick_think_llm": PAPER_CONFIG["quick_think_llm"],
            "max_debate_rounds": PAPER_CONFIG["max_debate_rounds"],
            "max_risk_discuss_rounds": PAPER_CONFIG["max_risk_discuss_rounds"],
            "data_vendors": {
                "core_stock_apis": "yfinance",
                "technical_indicators": "yfinance",
                "fundamental_data": "yfinance",
                "news_data": "rss",
            },
        }
    )
    return TradingAgentsGraph(debug=False, config=cfg)


def _control_action(action: str) -> dict[str, Any]:
    if action != "status" and not _mutating_controls_enabled():
        return {
            "ok": False,
            "error": "Mutating controls are disabled. Set TRADING_MODE=paper and BOT_ENABLED=true to enable them.",
        }
    portfolio = _portfolio()
    if action == "status":
        return _capture_output(paper_trader.cmd_status, portfolio)
    if action == "price_check":
        return _capture_output(paper_trader.run_price_check, portfolio)
    if action == "eod_summary":
        return _capture_output(paper_trader.run_eod_summary, portfolio)
    if action == "eod_close":
        return _capture_output(paper_trader.run_eod_close, portfolio)
    if action == "morning_analysis":
        ta = _make_trading_graph()
        return _capture_output(paper_trader.run_morning_analysis, ta, portfolio)
    return {"ok": False, "error": f"Unknown action: {action}"}


def _bot_status() -> dict[str, Any]:
    global BOT_PROCESS
    if BOT_PROCESS and BOT_PROCESS.poll() is None:
        return {"running": True, "pid": BOT_PROCESS.pid}
    BOT_PROCESS = None
    return {"running": False, "pid": None}


def _start_bot() -> dict[str, Any]:
    global BOT_PROCESS
    if not _mutating_controls_enabled():
        return {
            "ok": False,
            "bot": _bot_status(),
            "message": "Bot start is disabled. Set TRADING_MODE=paper and BOT_ENABLED=true.",
        }
    if _bot_status()["running"]:
        return {"ok": True, "bot": _bot_status(), "message": "Paper trader is already running."}
    if _lock_file().exists():
        return {
            "ok": False,
            "bot": _bot_status(),
            "message": f"Paper trader lock exists at {_lock_file()}; another instance may already be running.",
        }
    env = os.environ.copy()
    logfile = _process_log_file()
    logfile.parent.mkdir(parents=True, exist_ok=True)
    stream = logfile.open("a", encoding="utf-8")
    BOT_PROCESS = subprocess.Popen(
        [sys.executable, str(ROOT / "paper_trader.py")],
        cwd=str(ROOT),
        stdout=stream,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    return {"ok": True, "bot": _bot_status(), "message": f"Started paper trader. Output: {logfile}"}


def _stop_bot() -> dict[str, Any]:
    global BOT_PROCESS
    if not BOT_PROCESS or BOT_PROCESS.poll() is not None:
        BOT_PROCESS = None
        return {"ok": True, "bot": _bot_status(), "message": "Paper trader is not running from this dashboard."}
    if os.name == "nt":
        BOT_PROCESS.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        BOT_PROCESS.terminate()
    try:
        BOT_PROCESS.wait(timeout=8)
    except subprocess.TimeoutExpired:
        BOT_PROCESS.kill()
        BOT_PROCESS.wait(timeout=5)
    BOT_PROCESS = None
    return {"ok": True, "bot": _bot_status(), "message": "Stopped paper trader."}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "TradingAgentsDashboard/1.0"

    def do_GET(self) -> None:
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self._send_json(_status_payload())
            return
        if parsed.path == "/api/logs":
            limit = int(parse_qs(parsed.query).get("limit", ["180"])[0])
            self._send_json({"lines": _tail_lines(_log_file(), min(limit, 1000))})
            return
        if parsed.path == "/api/states":
            self._send_json({"states": [_compact_state(path) for path in _state_log_files()[:30]]})
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        payload = json.loads(body.decode("utf-8") or "{}") if body else {}
        if parsed.path == "/api/control":
            self._send_json(_control_action(str(payload.get("action", ""))))
            return
        if parsed.path == "/api/bot":
            action = str(payload.get("action", ""))
            if action == "start":
                self._send_json(_start_bot())
            elif action == "stop":
                self._send_json(_stop_bot())
            else:
                self._send_json({"ok": False, "error": f"Unknown bot action: {action}"}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _serve_static(self, request_path: str) -> None:
        path = STATIC_DIR / ("index.html" if request_path in {"", "/"} else request_path.lstrip("/"))
        try:
            path = path.resolve()
            if STATIC_DIR.resolve() not in path.parents and path != STATIC_DIR.resolve():
                raise FileNotFoundError
            if not path.exists() or not path.is_file():
                raise FileNotFoundError
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(path.suffix, "application/octet-stream")
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _check_auth(self) -> bool:
        if _authorized(self.headers):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="TradingAgents Dashboard"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"error":"Unauthorized"}')
        return False

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="TradingAgents paper trading dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    for path in (_state_file(), _log_file(), _lock_file()):
        path.parent.mkdir(parents=True, exist_ok=True)
    paper_trader._setup_logging(str(_log_file()))
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not _auth_required():
        raise RuntimeError(
            "Refusing to bind dashboard publicly without auth. "
            "Set DASHBOARD_PASSWORD or DASHBOARD_TOKEN."
        )
    with CONFIG_LOCK:
        os.chdir(ROOT)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
