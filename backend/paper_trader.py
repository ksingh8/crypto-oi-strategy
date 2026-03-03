"""
Paper Trader — simulates trade execution and monitors open positions for TP/SL hits.
"""
import logging
from datetime import datetime
import database as db

logger = logging.getLogger(__name__)

# Max concurrent open trades per symbol
MAX_OPEN_TRADES = 2

def should_open_trade(signal, symbol: str, config: dict) -> tuple[bool, str]:
    """Check if we should open a new trade based on signal + risk rules."""
    if signal.direction == "NEUTRAL":
        return False, "No signal"
    
    if signal.confidence < config.get("min_confidence", 40):
        return False, f"Confidence too low ({signal.confidence})"
    
    open_trades = db.get_open_trades(symbol)
    
    # Don't open if already at max
    if len(open_trades) >= MAX_OPEN_TRADES:
        return False, f"Max open trades reached ({MAX_OPEN_TRADES})"
    
    # Don't open same direction if already have one open
    same_dir = [t for t in open_trades if t["direction"] == signal.direction]
    if same_dir:
        return False, f"Already have open {signal.direction} trade"
    
    return True, "OK"

def open_paper_trade(signal, symbol: str, config: dict) -> dict | None:
    """Open a new paper trade."""
    should, reason = should_open_trade(signal, symbol, config)
    if not should:
        logger.info(f"Skipping trade: {reason}")
        return None
    
    position_size = config.get("position_size_usd", 100)
    trade_id = db.open_trade(signal, symbol, position_size)
    
    logger.info(f"Opened paper {signal.direction} trade #{trade_id} @ {signal.entry_price} "
                f"TP={signal.tp_price} SL={signal.sl_price} (confidence: {signal.confidence})")
    
    return {"trade_id": trade_id, "direction": signal.direction, "entry": signal.entry_price}

def check_open_positions(current_price: float, symbol: str):
    """Check all open trades for TP/SL hits."""
    open_trades = db.get_open_trades(symbol)
    closed = []
    
    for trade in open_trades:
        hit = None
        
        if trade["direction"] == "LONG":
            if current_price >= trade["tp_price"]:
                hit = ("TP", trade["tp_price"])
            elif current_price <= trade["sl_price"]:
                hit = ("SL", trade["sl_price"])
        else:  # SHORT
            if current_price <= trade["tp_price"]:
                hit = ("TP", trade["tp_price"])
            elif current_price >= trade["sl_price"]:
                hit = ("SL", trade["sl_price"])
        
        if hit:
            reason, exit_price = hit
            db.close_trade(trade["id"], exit_price, reason)
            pnl_pct = ((exit_price - trade["entry_price"]) / trade["entry_price"] * 100
                       if trade["direction"] == "LONG"
                       else (trade["entry_price"] - exit_price) / trade["entry_price"] * 100)
            logger.info(f"Closed trade #{trade['id']} via {reason} @ {exit_price} | PnL: {pnl_pct:.2f}%")
            closed.append({"trade_id": trade["id"], "reason": reason, "pnl_pct": pnl_pct})
    
    return closed
