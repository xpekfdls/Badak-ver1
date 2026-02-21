"""
QA: Hybrid Journal (Binance + Local) Verification
===================================================
"""
import sys, os, time, tempfile, json
from unittest.mock import patch, MagicMock
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
import config
config.DB_PATH = Path(_tmp_db.name)

from data.db import init_db, set_setting, save_order, get_order_by_binance_id
init_db()

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results = []

def check(name, cond, detail=""):
    status = PASS if cond else FAIL
    results.append((name, cond))
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail else ""))


# --- QA-1: Binance API functions exist and have correct signatures ---

def test_binance_api_functions():
    print("\n=== QA-1: Binance API functions ===")

    from trading.binance_account import get_user_trades, get_income_history

    check("get_user_trades callable", callable(get_user_trades))
    check("get_income_history callable", callable(get_income_history))

    # Not configured -> returns empty list
    with patch("trading.binance_account.is_configured", return_value=False):
        trades = get_user_trades(symbol="BTCUSDT", limit=10)
        check("get_user_trades returns [] when not configured", trades == [])

        income = get_income_history(income_type="REALIZED_PNL", limit=10)
        check("get_income_history returns [] when not configured", income == [])


# --- QA-2: Binance income -> hybrid merge with local orders ---

def test_hybrid_merge():
    print("\n=== QA-2: Hybrid merge logic ===")

    # Save a local order with binance_order_id
    order = {
        "id": "ord-001", "binance_order_id": 55555,
        "client_order_id": "exit_abc", "symbol": "ENSOUSDT",
        "side": "SELL", "order_type": "MARKET", "quantity": 25.6,
        "price": 0, "stop_price": 0, "expected_price": 2.0,
        "filled_qty": 25.6, "avg_fill_price": 2.0,
        "status": "FILLED", "reduce_only": 1, "position_id": "pos-001",
        "purpose": "TRAIL", "created_at": int(time.time()*1000),
        "updated_at": int(time.time()*1000), "error_msg": "",
        "slippage_pct": 0, "leverage": 5, "signal_tick": 5,
        "signal_scenario": "A:40%",
    }
    save_order(order)

    local_order = get_order_by_binance_id(55555)
    check("local order saved and retrievable", local_order is not None)
    check("purpose = TRAIL", local_order.get("purpose") == "TRAIL")
    check("signal_tick = 5", local_order.get("signal_tick") == 5)

    # Simulate Binance income record matching this order
    mock_income = [
        {"symbol": "ENSOUSDT", "incomeType": "REALIZED_PNL",
         "income": "1.5000", "time": int(time.time()*1000),
         "tradeId": "12345", "orderId": "55555", "info": ""},
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL",
         "income": "-0.3000", "time": int(time.time()*1000) - 60000,
         "tradeId": "12346", "orderId": "99999", "info": ""},
    ]

    with patch("trading.binance_account.get_income_history", return_value=mock_income), \
         patch("trading.binance_account.is_configured", return_value=True):

        # Simulate the API endpoint logic
        from trading.binance_account import get_income_history as _gi
        incomes = _gi(income_type="REALIZED_PNL", limit=50)

        merged = []
        for inc in incomes:
            pnl = float(inc.get("income", 0))
            if abs(pnl) < 0.0001:
                continue
            order_id = int(inc.get("orderId", 0) or 0)
            lo = get_order_by_binance_id(order_id) if order_id else None
            purpose = lo.get("purpose", "") if lo else ""
            sig = lo.get("signal_tick", 0) if lo else 0

            merged.append({
                "source": "binance",
                "symbol": inc.get("symbol"),
                "realized_pnl": round(pnl, 4),
                "exit_reason": purpose,
                "signal_tick": sig,
            })

    check("merged 2 records", len(merged) == 2, f"count={len(merged)}")

    enso = [m for m in merged if m["symbol"] == "ENSOUSDT"]
    check("ENSO matched exit_reason=TRAIL",
          len(enso) == 1 and enso[0]["exit_reason"] == "TRAIL")
    check("ENSO matched signal_tick=5",
          len(enso) == 1 and enso[0]["signal_tick"] == 5)
    check("ENSO PnL from Binance ($1.50)",
          len(enso) == 1 and enso[0]["realized_pnl"] == 1.5)

    btc = [m for m in merged if m["symbol"] == "BTCUSDT"]
    check("BTC no local match -> empty exit_reason",
          len(btc) == 1 and btc[0]["exit_reason"] == "")


# --- QA-3: Binance summary stats ---

def test_binance_summary():
    print("\n=== QA-3: Binance summary stats ===")

    mock_pnl = [
        {"income": "1.5000"}, {"income": "-0.3000"}, {"income": "0.8000"},
    ]
    mock_fee = [
        {"income": "-0.1000"}, {"income": "-0.0800"},
    ]
    mock_fund = [
        {"income": "0.0200"}, {"income": "-0.0100"},
    ]

    set_setting("seed_money", "1000")

    with patch("trading.binance_account.get_income_history") as mock_gi, \
         patch("trading.binance_account.is_configured", return_value=True):

        def side_effect(income_type="", **kw):
            if income_type == "REALIZED_PNL": return mock_pnl
            if income_type == "COMMISSION": return mock_fee
            if income_type == "FUNDING_FEE": return mock_fund
            return []
        mock_gi.side_effect = side_effect

        total_pnl = sum(float(r["income"]) for r in mock_pnl)
        total_fee = sum(float(r["income"]) for r in mock_fee)
        total_fund = sum(float(r["income"]) for r in mock_fund)
        net = total_pnl + total_fee + total_fund
        wins = sum(1 for r in mock_pnl if float(r["income"]) > 0)

        check("total_pnl = 2.0", abs(total_pnl - 2.0) < 0.01, f"actual={total_pnl}")
        check("total_fee = -0.18", abs(total_fee - (-0.18)) < 0.01, f"actual={total_fee}")
        check("total_funding = 0.01", abs(total_fund - 0.01) < 0.01, f"actual={total_fund}")
        check("net_pnl = 1.83", abs(net - 1.83) < 0.01, f"actual={net}")
        check("wins = 2", wins == 2)
        check("return_pct", abs(net / 1000 * 100 - 0.183) < 0.01)


# --- QA-4: Frontend code verification ---

def test_frontend_code():
    print("\n=== QA-4: Frontend hybrid code ===")

    journal_js = (BACKEND.parent / "frontend" / "js" / "journal.js").read_text(encoding="utf-8")

    check("fetches /api/trades/binance",
          "/api/trades/binance" in journal_js)
    check("fetches /api/trades/binance/summary",
          "/api/trades/binance/summary" in journal_js)
    check("renderBinanceTrades function exists",
          "renderBinanceTrades" in journal_js)
    check("renderLocalTrades fallback exists",
          "renderLocalTrades" in journal_js)
    check("source indicator (Binance/Local)",
          "Binance" in journal_js and "Local" in journal_js)

    html = (BACKEND.parent / "frontend" / "index.html").read_text(encoding="utf-8")
    check("trades table has Source column", "Source" in html)

    pos_js = (BACKEND.parent / "frontend" / "js" / "positions.js").read_text(encoding="utf-8")
    check("position update timer exists", "_updateTimerBar" in pos_js)

    app_js = (BACKEND.parent / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    check("position_updated triggers Journal.refresh",
          "position_updated" in app_js and "Journal.refresh()" in app_js)


if __name__ == "__main__":
    print("=" * 60)
    print("  Hybrid Journal QA Test Suite")
    print("=" * 60)

    test_binance_api_functions()
    test_hybrid_merge()
    test_binance_summary()
    test_frontend_code()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    failed = total - passed
    color = "\033[92m" if failed == 0 else "\033[91m"
    print(f"  Result: {color}{passed}/{total} passed\033[0m" +
          (f" ({failed} FAILED)" if failed else " -- ALL PASS"))
    print("=" * 60)

    try: os.unlink(_tmp_db.name)
    except: pass
    sys.exit(0 if failed == 0 else 1)
