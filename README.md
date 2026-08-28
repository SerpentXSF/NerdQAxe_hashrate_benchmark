# NerdQAxe++ Hashrate Benchmark (health-aware)

An automated voltage/frequency tuner for the **NerdQAxe++** (4x BM1370, NerdOS
firmware). It sweeps core voltage and frequency, measures each setting, and applies
the best one - picking for efficiency (J/TH) or raw hashrate while staying inside a
power budget, a thermal limit, and a **hashrate-health** stability gate.

It is a NerdQAxe port of an error-aware BitAxe benchmark. NerdOS exposes no
`errorPercentage`, so instead of gating on an ASIC error rate this tool gates on the
two health signals NerdOS *does* expose:

- **Hashrate health** - the delivered hashrate as a fraction of theoretical
  (`frequency x smallCoreCount x asicCount / 1000`). An under-fed (starved)
  undervolt silently drops effective hashrate while the chip reports no errors, so a
  droop below the floor is the primary instability signal.
- **Duplicate hardware nonces** (`duplicateHWNonces`) - a secondary signal; a stable
  chip holds it at zero.

## Why power is the primary limit

On a NerdQAxe++ the fan runs a PID loop that holds the chip near its target
temperature, so temperature stays regulated and **power is usually the binding
constraint**, not heat. The default power cap is **100 W** - the 124 W stock PSU at
its 80% continuous rating. Raise it with `--max-power` if you run a larger supply
(the board's XT30 / TPS546D24A design and the device's reported `maxPower` allow
more for short bursts).

## Modes

- `grid` (default) - full sweep, health/power/thermal-aware.
- `refine` - rescue an unstable or starved chip: hold a frequency and sweep voltage
  up until the hashrate stops drooping; if the setting hits the power or thermal cap
  first, drop the frequency and retry (lowering frequency is what helps once power
  or cooling is the limit).
- `efficiency` - trim voltage down on an already-healthy miner to cut power/heat.
- `--check` - read-only: measure the current setting once and report. No changes,
  no reboot.

## Useful flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--max-power` | 100 | Power cap in W (24/7 safe on the stock PSU; raise for a bigger one) |
| `--min-health` | 0.95 | Hashrate-health floor (fraction of theoretical) |
| `--max-temp` | 70 | Chip temperature cutoff in C |
| `--prefer` | efficiency | `efficiency` (lowest J/TH) or `hashrate` (highest first) among passers |
| `--mode` | grid | `grid` / `refine` / `efficiency` |
| `--soak` | 0 | After applying, watch the winner at steady state for N minutes |
| `--bracket` | off | refine: also test one frequency step down for J/TH |
| `--benchmark-time` | 600 | Per-combo window in seconds |
| `--resume` | off | Reload prior results for this IP and skip tested combos |
| `--no-health-gate` | off | Report health but do not gate on it |

```bash
python nerdqaxe_hashrate_benchmark.py 192.168.1.50 --mode refine --bracket
python nerdqaxe_hashrate_benchmark.py 192.168.1.50 --check          # read-only
python nerdqaxe_hashrate_benchmark.py 192.168.1.50 --max-power 115 --prefer hashrate
```

## Real-world result (NerdQAxe++ Rev 7, XT30/TPS546)

Tuned against a live unit at a 100 W continuous budget. Stock stored settings vs.
the tuned result:

| Setting | Hashrate | Power | Efficiency | Notes |
|---------|----------|-------|------------|-------|
| Stock 780/1200 | 6.30 TH/s | 99 W | 15.78 J/TH | |
| **Tuned 800/1180** | **6.36 TH/s** | 98.7 W | **15.52 J/TH** | +hashrate, -power, cooler |

For reference the chip scales cleanly to ~6.87 TH/s at 111 W (850/1220) if you raise
the power cap and have the PSU/cooling for it - the limiter there is the 100 W
continuous budget and the fans, not silicon.

## Safety

All the device-side safeguards are preserved: chip-temp and VR-temp cutoffs, an
input-voltage window, the power cutoff, a minimum sample count, and voltage/frequency
clamps. A combo already over a limit during the post-apply stabilization window is
dropped there rather than wasting the sample window. The best setting (or the
device's starting settings, if nothing measured cleanly) is restored on exit and on
Ctrl+C, and the tool warns rather than claiming success if a restore can't be
confirmed.

## fleet.py

A companion `fleet.py` reads `/api/system/info` from many miners and prints a
flagged status table (hashrate, **health**, temp, VR, both fans, J/TH), and can run
the benchmark across a fleet with before/after deltas. Rows are flagged for hashrate
droop, duplicate nonces, a hot chip, or both fans pegged.

```bash
python fleet.py 192.168.1.50 192.168.1.51
python fleet.py --file miners.txt --run "--mode refine --bracket"
```

## Requirements

Python 3 with `requests` (`pip install -r requirements.txt`). Single file, standard
library plus `requests`.

## Credits

This tool descends from the Bitaxe hashrate-benchmark lineage:

- [mrv777/Bitaxe-Hashrate-Benchmark](https://github.com/mrv777/Bitaxe-Hashrate-Benchmark)
  — the original Bitaxe hashrate benchmark (GPL-3.0) this ultimately derives from.
- [SerpentXSF/Bitaxe-Hashrate-Benchmark](https://github.com/SerpentXSF/Bitaxe-Hashrate-Benchmark)
  — the error-aware fork of mrv777's tool; this repo adapts its error/thermal-gated
  selection logic for the NerdQAxe++ (4× BM1370, NerdOS API differences).

## Support this project

Everything here is free and open source under GPL-3.0, and it stays that way -
there is no paid tier and nothing is held back. If it has been useful to you and
you want to say thanks, it is very much appreciated. Please never feel obliged.

**Bitcoin** (on-chain)

```
bc1qeepmx84606m0fphpuvlcafz9ukfmgyrlxjkt72
```

**USDC** - **Solana network only**

```
JAY1A2kXYY8jLzedyXFxJMoPZ5hS6Ja9K2qDPtx64Lbs
```

> Send USDC on the **Solana** network only. USDC sent to this address over
> Ethereum, Polygon, or any other chain will be **permanently lost** - that is
> how the networks work and it cannot be reversed.

## License

GPL-3.0. See [LICENSE](LICENSE). As a derivative of the GPL-3.0 tools above, this
project is licensed GPL-3.0 and preserves their attribution (see Credits).
