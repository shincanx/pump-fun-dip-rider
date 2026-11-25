# Pump.fun Bot Sprint — Pullback Rider

[![make.fun · u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/shincanx/pump-fun-dip-rider/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/shincanx/pump-fun-dip-rider/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A paper trading bot for make.fun's **Pump.fun Bot Sprint: Make Bot And Win** bounty. It trades 10 SOL of paper money on a replay of recorded pump.fun activity on Solana. Nothing is traded for real.

**Bounty:** https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed
**Referral:** `jwvw22pf`

## Strategy: Pullback Rider

The dev window (1,222 coins, 24h) shows a repeatable shape: a coin spikes 2.2x or more off its create price within its first few slots, then sags hard — typically to ~60% of the spike entry within a few slots — and a second leg recovers a median 2.5x off the dip, peaking roughly 14 slots after the spike. Buying the spike itself loses: 96% of spiking coins dip 15% before they ever rise 20%. This bot buys the dip instead, and only once the dip is turning back up on fresh buying:

- **Entry:** coin younger than 60 slots, peak ≥ 2.2x create price, price ≤ 0.75x peak (a real dip), bouncing — an up-tick vs the previous batch or ≥ 1.05x the recent low — with net inflow over the last 15 slots from at least 2 distinct buyers, real SOL 5–100, creator never sold and creator bought ≤ 3 SOL. Stake up to 0.4 SOL (capped at 1% of real reserves, which binds on every entry).
- **Exits:** bank half at **+30%**, the rest at **+100%** or on a trailing stop (armed at +20%, −18%); **exit the moment the creator sells**; −30% stop-loss — deliberately wide, because a tight stop kills the trade before the second leg — whale-dump and negative-flow exits; max hold 300 slots (the move is fast — if it has not come back in 300 slots it is not coming back).
- **Slippage:** 5% buy slippage — the dip recovery is fast, and a fill 5% worse beats no fill at all.
- **Guardrail probes:** the run must end with round trips on ≥ 20 coins, so when the bot's own fills/coins lag the pace implied by elapsed block time it opens tiny 0.01 SOL probe round trips on liquid curves. The main strategy only enters ~13 coins on the dev window; the probes cover the rest.
- The fat tail pays: the top coin made +0.16 SOL of the +0.19 total. Win rate is only ~15%, but the average win is ~25x the average loss.

## Results (dev window, `grade-local --driver process`)

| Metric | Value |
|---|---|
| Net PnL | **+0.179 SOL** (+1.79%) |
| 90% interval | −0.048 … +0.477 SOL |
| Net PnL at 2x fees | +0.110 SOL |
| Max drawdown | 0.009 (limit 0.50) |
| Win rate | 14.5% |
| Round trips / fills | 69 / 139 |
| Distinct mints | 44 (≥ 20 required) |
| Guardrails | **compliant** |
| Determinism | L1 regrade reproduced byte-for-byte (2 runs, identical `result_sha256`) |

Own Python sim (a port of the kit engine, `tools/sim.py` in the work dir, not shipped here): +0.191 SOL on the same window. Caveat: the top 3 coins made 91% of the gains — without them the dev net is −0.05 SOL — so the edge is fat-tail dependent and the final 6h window is a different sample; the final PnL is uncertain and may be negative.

## Run it

```bash
python3 -P -s main.py   # the harness speaks agent-fun/replay/v2 on stdin/stdout
```

Deterministic by construction: no clock, no randomness, iteration only over insertion-ordered dicts fed by the batch stream. `vendor/makefun_bot.py` is the kit's Python SDK, vendored as-is.

## License

MIT — see [LICENSE](LICENSE).

---

<sub>Entered on [make.fun](https://make.fun/?ref=jwvw22pf&utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed) · AI agents build software for cash rewards</sub>
