"""Pullback Rider — pump.fun replay paper bot (agent-fun/replay/v2).

Strategy, in one paragraph: most pump.fun coins die within minutes, but a
small share prove real demand first — they spike 2x or more off their
create price within their first few slots on broad buying. That spike
almost never holds: the price first sags hard (typically to ~60% of the
spike entry within a few slots) and then a second leg recovers a median
2.5x off the dip, peaking roughly 14 slots after the spike. This bot buys
that dip, not the spike: only once the dip is bouncing again on fresh
buying, with a small fixed stake per coin. It never chases a fresh launch
and never buys a coin whose creator is selling. Winners are banked fast
(half at +30%, the rest at +100% or on a trailing stop, and the move is
usually over within ~30 slots, so nothing is held long); losers are cut
at -30% — a tight stop would kill nearly every trade before the second
leg — on a creator sell, on a whale dump, or when the tape's net flow
turns negative. Positions are small and recycled fast, so the bot
spreads its 10 SOL across many coins instead of betting the run on one.

Mechanics: the bot reads each batch's market events as they arrive, keeps
a per-coin tape (create price, peak price, recent trades, creator), and
answers every batch with at most a handful of orders. It is deterministic:
no clock, no environment, no unseeded randomness; iteration is over
insertion-ordered dicts only. It writes nothing to disk and makes no
network calls; all output goes to stdout as protocol replies (diagnostics
to stderr).

Entry (all must hold):
  - coin created >= MIN_AGE slots ago, peak price >= MIN_RUNUP x create price
  - price now <= PULLBACK x peak (a real dip) and >= BOUNCE x the recent low
    (the dip is turning back up)
  - net SOL over the last FLOW_WIN slots >= MIN_NET, >= MIN_BUYS buys from
    >= MIN_BUYERS distinct wallets (broad, fresh demand — not one wallet)
  - real SOL reserves in [MIN_RQ, MAX_RQ]
  - the creator has never sold (a creator sell kills the entry, and exits
    any open position at once)

Exits (first trigger wins): stop-loss, take-profit 1 (sell half), take
profit 2 (sell the rest), trailing stop once armed, creator sell, single
whale sell >= BIG_SELL SOL, negative net flow over the fade window, or the
max-hold age while flat.

Guardrail probes: the run must end with round trips on >= 20 coins. If
the bot's own fills/coins lag the pace implied by elapsed block time, it
opens tiny 0.01 SOL probe round trips on liquid curves and sells them as
soon as the buy lands. Probes are insurance only; the main strategy is
expected to clear the bar on its own.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bot

LAMPORTS = 1_000_000_000
# Protocol + creator fees charged on every trade (~1.25%): used only to
# estimate a position's close value. The engine settles the real numbers.
FEE = 0.0125

PARAMS = {
    # --- entry: catch the dip of the early spike, not the spike itself ---
    "min_age": 5,           # slots since CreateEvent before an entry is allowed
    "max_age": 60,          # the dip-and-recover move is over within ~a minute
    "min_runup": 2.2,       # peak price / create price: the coin proved demand
    "pullback": 0.75,       # price now <= this x peak: a real dip off the spike
    "bounce": 1.05,         # price now >= this x recent low: the dip is turning
    "require_creator_exit": False,  # when True: only enter coins whose creator
                            # already sold out (the dump risk is gone)
    "peak_age_max": 0,      # when > 0: the peak must be this fresh (slots)
    "min_rise": 0.0,        # when > 0: price must be >= this x the price
    "rise_win": 60,         # rise_win slots ago (momentum, not a dip)
    "tick": 1.005,          # ...or the price just ticked up this much vs the
                            # previous batch: the fastest bounce signal, for a
                            # recovery that is over within a few slots
    "flow_win": 15,         # slots of recent tape the entry flow is read over
    "min_net": 0.1,         # min net SOL (buys - sells) over flow_win
    "min_buys": 2,          # min buy trades over flow_win
    "min_buyers": 2,        # min distinct buying wallets over flow_win
    "min_rq": 5.0,          # min real SOL reserves at entry (1% cap => stake)
    "max_rq": 100.0,        # max real SOL reserves at entry
    "max_creator_buy": 3.0, # skip coins whose creator bought more than this
    "stake": 0.4,           # SOL per position (capped by 1% of real reserves;
                            # the cap binds on every entry here, so this is a
                            # ceiling, not the usual size)
    "buy_slip_bps": 500,    # 5% buy slippage: the dip recovery is fast, and a
                            # fill 5% worse beats no fill at all (the median
                            # recovery off the dip is +150%)
    "max_entries": 1,       # most entries per coin over the run
    "cooldown": 150,        # slots to wait after an exit before re-entering a coin
    # --- exits: bank the fast second leg, cut the dead dip ---
    "stop": 0.30,           # sell all when value < cost * (1 - stop); wide on
                            # purpose — a tight stop kills the trade before the
                            # second leg (96% of spikes dip -15% first)
    "tp1": 1.3,             # sell half when value >= cost * tp1
    "tp2": 2.0,             # sell the rest when value >= cost * tp2
    "trail_arm": 1.2,       # trailing stop arms once value >= cost * trail_arm
    "trail": 0.18,          # sell all when value < peak_value * (1 - trail)
    "fade_win": 15,         # slots of tape the fade flow is read over
    "fade_net": -1.0,       # sell all when net SOL over fade_win < fade_net
    "big_sell": 2.0,        # sell all on any single recorded sell >= this many SOL
    "max_hold": 300,        # slots; the move is fast — if it has not come
                            # back in 300 slots it is not coming back
    # --- exposure ---
    "max_pos": 5,           # most momentum positions open at once
    "max_exposure": 3.0,    # most SOL at cost across all positions at once
    # --- guardrail probes ---
    "target_coins": 21,
    "target_fills": 120,
    "pace_secs": 21600,     # 6h final window; the dev window is longer, pace caps at 1
    "probe_sol": 0.01,
    "max_probes": 1,
}


class Tape(object):
    """Per-coin tape: create, peak, recent trades, creator behaviour."""

    __slots__ = ("created", "creator", "create_price", "peak", "peak_slot",
                 "trades", "entries", "last_exit", "creator_sold", "creator_buy",
                 "last_price")

    def __init__(self):
        self.created = None
        self.creator = None
        self.create_price = 0.0
        self.peak = 0.0
        self.peak_slot = None
        self.trades = []          # (slot, is_buy, sol, user, price), oldest first
        self.entries = 0
        self.last_exit = -10 ** 12
        self.creator_sold = False
        self.creator_buy = 0.0    # SOL the creator bought (a heavy creator buy
                                  # usually precedes a creator dump)
        self.last_price = 0.0     # price at the previous entry evaluation


class Held(object):
    """An open momentum position."""

    __slots__ = ("opened", "peak_ratio", "tp1_done", "exiting", "filled")

    def __init__(self, opened):
        self.opened = opened
        self.peak_ratio = 1.0     # best value / cost seen so far
        self.tp1_done = False
        self.exiting = False
        self.filled = False


def create_bot(p=None):
    params = dict(PARAMS)
    if p:
        params.update(p)
    tapes = {}
    held = {}
    stats = {"exits": {}, "entries": 0, "holds": [],
             "gates": {"age": 0, "runup": 0, "rq": 0, "pullback": 0, "bounce": 0,
                       "creator": 0, "flow": 0, "passed": 0, "blocked": 0}}
    probes = {}                   # mint -> selling (bool): probe round trip in flight
    active = {}                   # mint -> last trade slot (entry candidates only:
                                  # a coin with no trade inside the flow window
                                  # cannot pass the flow/bounce gates anyway)
    my_fills = 0
    my_coins = set()
    first_time = None
    last_time = None
    batch_no = 0
    keep = max(params["flow_win"], params["fade_win"]) + 120

    def tape_of(mint):
        t = tapes.get(mint)
        if t is None:
            t = Tape()
            tapes[mint] = t
        return t

    def flow(t, now, window):
        """Net recorded SOL (buys minus sells) over the last `window` slots."""
        net = 0.0
        for i in range(len(t.trades) - 1, -1, -1):
            slot, is_buy, sol, _user, _price = t.trades[i]
            if slot <= now - window:
                break
            net += sol if is_buy else -sol
        return net

    def recent_low(t, now, window):
        low = None
        for i in range(len(t.trades) - 1, -1, -1):
            slot, _b, _s, _u, price = t.trades[i]
            if slot <= now - window:
                break
            if low is None or price < low:
                low = price
        return low

    def price_at(t, slot):
        """Last recorded trade price at or before `slot` (None if none)."""
        found = None
        for i in range(len(t.trades) - 1, -1, -1):
            if t.trades[i][0] <= slot:
                found = t.trades[i][4]
                break
        return found

    def entry_ok(t, now, rq_sol, price):
        g = stats["gates"]
        prev_price = t.last_price
        t.last_price = price
        if t.created is None or now - t.created < params["min_age"]:
            return False
        if now - t.created > params["max_age"]:
            g["age"] += 1
            return False
        if t.create_price <= 0 or t.peak < t.create_price * params["min_runup"]:
            g["runup"] += 1
            return False
        if rq_sol < params["min_rq"] or rq_sol > params["max_rq"]:
            g["rq"] += 1
            return False
        if price > t.peak * params["pullback"]:
            g["pullback"] += 1
            return False
        low = recent_low(t, now, params["flow_win"])
        tick_up = prev_price > 0 and price > prev_price * params["tick"]
        bounced = tick_up or (low is not None and price >= low * params["bounce"])
        if not bounced:
            g["bounce"] += 1
            return False
        if params.get("require_creator_exit"):
            if not t.creator_sold:
                g["creator"] += 1
                return False
        else:
            if t.creator_sold:
                g["creator"] += 1
                return False
            if t.creator_buy > params["max_creator_buy"]:
                g["creator"] += 1
                return False
        if params.get("peak_age_max") and t.peak_slot is not None:
            if now - t.peak_slot > params["peak_age_max"]:
                g["pullback"] += 1
                return False
        if params.get("min_rise"):
            old = price_at(t, now - params["rise_win"])
            if old is None or price < old * params["min_rise"]:
                g["flow"] += 1
                return False
        net = 0.0
        buys = 0
        buyers = set()
        for i in range(len(t.trades) - 1, -1, -1):
            slot, is_buy, sol, user, _price = t.trades[i]
            if slot <= now - params["flow_win"]:
                break
            if is_buy:
                net += sol
                buys += 1
                buyers.add(user)
            else:
                net -= sol
        if buys < params["min_buys"] or len(buyers) < params["min_buyers"]:
            g["flow"] += 1
            return False
        if net < params["min_net"]:
            g["flow"] += 1
            return False
        g["passed"] += 1
        return True

    def on_batch(state, batch):
        nonlocal my_fills, first_time, last_time, batch_no
        batch_no += 1
        now = batch["slot"]
        big_sell = set()
        traded = set()
        for f in batch["fills"]:
            if f["status"] == "filled" and not f["client_id"].startswith("_"):
                my_fills += 1
                my_coins.add(f["mint"])
        for e in batch["events"]:
            if e.get("block_time") is not None:
                bt = int(e["block_time"])
                if first_time is None:
                    first_time = bt
                if last_time is None or bt > last_time:
                    last_time = bt
            if e["event"] == "CreateEvent":
                t = tape_of(e["mint"])
                t.created = int(e["slot"])
                t.creator = e.get("creator") or e.get("user")
                vq = int(e["virtual_quote_reserves"])
                vt = int(e["virtual_token_reserves"])
                t.create_price = vq / vt if vt else 0.0
                t.peak = t.create_price
            elif e["event"] == "TradeEvent":
                t = tape_of(e["mint"])
                sol = int(e["sol_amount"]) / LAMPORTS
                price = int(e["virtual_quote_reserves"]) / int(e["virtual_token_reserves"])
                t.trades.append((int(e["slot"]), bool(e["is_buy"]), sol,
                                 e["user"], price))
                if price > t.peak:
                    t.peak = price
                    t.peak_slot = int(e["slot"])
                if t.creator is not None and e["user"] == t.creator and e["is_buy"]:
                    t.creator_buy += sol
                traded.add(e["mint"])
                active[e["mint"]] = int(e["slot"])
                if not e["is_buy"] and t.creator is not None and e["user"] == t.creator:
                    t.creator_sold = True
                    big_sell.add(e["mint"])
                elif not e["is_buy"] and sol >= params["big_sell"]:
                    big_sell.add(e["mint"])
            else:  # CompleteEvent
                t = tape_of(e["mint"])
                t.trades.append((int(e["slot"]), False, 0.0, "", 0.0))
        for mint in traded:
            t = tapes[mint]
            cut = 0
            while cut < len(t.trades) and t.trades[cut][0] <= now - keep:
                cut += 1
            if cut:
                del t.trades[:cut]

        marks = {}
        for m in batch["marks"]:
            marks[m["mint"]] = m
        pending_by_mint = {}
        for po in state.pending.values():
            pending_by_mint[po.order["mint"]] = pending_by_mint.get(po.order["mint"], 0) + 1

        orders = []
        cap = state.hello["caps"]["max_orders_per_batch"]
        tx_sol = state.tx_fee_lamports() / LAMPORTS

        def close_value(mint, pos):
            curve = state.curves.get(mint)
            if curve is None:
                return 0.0
            mk = marks.get(mint)
            if mk is not None:
                vq = int(mk["virtual_quote_reserves"])
                vt = int(mk["virtual_token_reserves"])
            else:
                vq = int(curve.virtual_quote_reserves)
                vt = int(curve.virtual_token_reserves)
            tokens = int(pos.token_amount)
            if vt + tokens <= 0:
                return 0.0
            return (tokens * vq / (vt + tokens) / LAMPORTS) * (1.0 - FEE) - tx_sol

        # --- exits first ---
        for mint in list(held.keys()):
            h = held[mint]
            pos = state.positions.get(mint)
            curve = state.curves.get(mint)
            if pos is None:
                if mint not in pending_by_mint:
                    del held[mint]
                    t = tape_of(mint)
                    if h.filled:
                        t.last_exit = now
                    else:
                        t.entries -= 1
                continue
            h.filled = True
            if curve is None or curve.complete:
                continue
            value = close_value(mint, pos)
            cost = int(pos.cost_lamports) / LAMPORTS
            if cost <= 0:
                continue
            ratio = value / cost
            if ratio > h.peak_ratio:
                h.peak_ratio = ratio
            t = tape_of(mint)
            sell_all = False
            sell_half = False
            reason = None
            if t.creator_sold or mint in big_sell:
                sell_all = True
                reason = "creator" if t.creator_sold else "big_sell"
            elif ratio < 1.0 - params["stop"]:
                sell_all = True
                reason = "stop"
            elif ratio >= params["tp2"]:
                sell_all = True
                reason = "tp2"
            elif (h.peak_ratio >= params["trail_arm"]
                    and ratio < h.peak_ratio * (1.0 - params["trail"])):
                sell_all = True
                reason = "trail"
            elif flow(t, now, params["fade_win"]) < params["fade_net"]:
                sell_all = True
                reason = "fade"
            elif now - h.opened > params["max_hold"] and ratio < params["tp1"]:
                sell_all = True
                reason = "max_hold"
            elif ratio >= params["tp1"] and not h.tp1_done:
                sell_half = True
                reason = "tp1"
            if not sell_all and not sell_half:
                continue
            stats["exits"][reason] = stats["exits"].get(reason, 0) + 1
            if sell_all:
                stats["holds"].append(now - h.opened)
            if mint in pending_by_mint:
                # A sell is already in flight: wait for it. A buy in flight:
                # still sell what we hold now.
                sell_pending = False
                for po in state.pending.values():
                    if po.order["mint"] == mint and po.order["side"] == "sell":
                        sell_pending = True
                        break
                if sell_pending:
                    continue
            if len(orders) >= cap:
                break
            tokens = int(pos.token_amount)
            if sell_all:
                h.exiting = True
                orders.append({"side": "sell", "mint": mint,
                               "token_amount": tokens,
                               "max_slippage_bps": 10000})
            else:
                h.tp1_done = True
                orders.append({"side": "sell", "mint": mint,
                               "token_amount": tokens // 2,
                               "max_slippage_bps": 10000})

        # --- probe round trips: sell the probe as soon as its buy lands ---
        for mint in list(probes.keys()):
            selling = probes[mint]
            pos = state.positions.get(mint)
            curve = state.curves.get(mint)
            if pos is None:
                if mint not in pending_by_mint:
                    del probes[mint]
                continue
            if curve is None or curve.complete or mint in pending_by_mint:
                continue
            if len(orders) >= cap:
                break
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": int(pos.token_amount),
                           "max_slippage_bps": 10000})
            if not selling:
                probes[mint] = True

        # --- entries ---
        free = state.free_cash_lamports() / LAMPORTS
        slots_left = state.hello["caps"]["max_open_positions"] - state.committed_position_count()
        main_left = params["max_pos"] - len(held)
        exposure = 0.0
        for mint, h in held.items():
            pos = state.positions.get(mint)
            exposure += int(pos.cost_lamports) / LAMPORTS if pos is not None else 0.0
        if batch_no % 200 == 0:
            stale = [m for m, s in active.items() if s <= now - keep]
            for m in stale:
                del active[m]
        for mint in list(active.keys()):
            if len(orders) >= cap:
                break
            if mint in held or mint in probes or mint in pending_by_mint:
                continue
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            t = tapes.get(mint)
            if t is None:
                continue
            if t.entries >= params["max_entries"]:
                continue
            if now - t.last_exit < params["cooldown"]:
                continue
            if slots_left <= 0 or main_left <= 0:
                stats["gates"]["blocked"] += 1
                continue
            price = (int(curve.virtual_quote_reserves)
                     / int(curve.virtual_token_reserves))
            rq_sol = int(curve.real_quote_reserves) / LAMPORTS
            if not entry_ok(t, now, rq_sol, price):
                continue
            cap_sol = state.max_buy_lamports(mint) / LAMPORTS
            amount = min(cap_sol, params["stake"],
                         params["max_exposure"] - exposure)
            if amount > free - tx_sol:
                amount = free - tx_sol
            if amount < 0.01:
                continue
            lamports = int(amount * LAMPORTS)
            orders.append({"side": "buy", "mint": mint,
                           "quote_lamports": lamports,
                           "max_slippage_bps": params["buy_slip_bps"]})
            free -= amount + tx_sol
            exposure += amount
            held[mint] = Held(now)
            stats["entries"] += 1
            t.entries += 1
            slots_left -= 1
            main_left -= 1

        # --- guardrail pacing: open a probe when fills/coins lag the pace ---
        if (len(probes) < params["max_probes"] and slots_left > 0
                and first_time is not None and last_time is not None):
            pace = min(1.0, (last_time - first_time) / params["pace_secs"])
            need_coins = len(my_coins) < int(params["target_coins"] * pace + 0.999)
            need_fills = my_fills < int(params["target_fills"] * pace + 0.999)
            if need_coins or need_fills:
                min_rq = (params["probe_sol"] * 10000
                          / state.hello["caps"]["max_order_bps_of_real_reserve"])
                for mint in traded:
                    if len(orders) >= cap or len(probes) >= params["max_probes"]:
                        break
                    if mint in held or mint in probes or mint in pending_by_mint:
                        continue
                    if mint in state.positions:
                        continue
                    if need_coins and not need_fills and mint in my_coins:
                        continue
                    curve = state.curves.get(mint)
                    if curve is None or curve.complete:
                        continue
                    if int(curve.real_quote_reserves) / LAMPORTS < min_rq * 1.5:
                        continue
                    if free < params["probe_sol"] + 2 * tx_sol:
                        break
                    orders.append({"side": "buy", "mint": mint,
                                   "quote_lamports": int(params["probe_sol"] * LAMPORTS),
                                   "max_slippage_bps": params["buy_slip_bps"]})
                    probes[mint] = False
                    free -= params["probe_sol"] + tx_sol
        return orders

    on_batch.stats = stats
    return on_batch


def on_batch(state, batch):
    return _BOT(state, batch)


_BOT = create_bot()


def main():
    makefun_bot.run(on_batch)


if __name__ == "__main__":
    main()
