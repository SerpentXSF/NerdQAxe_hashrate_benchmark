import requests
import time
import json
import csv
import glob
import os
import signal
import sys
import argparse
from datetime import datetime

START_TIME = datetime.now().strftime("%Y-%m-%d_%H%M%S")

# ANSI Color Codes
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"

# Configuration - NerdQAxe++ (4x BM1370). Defaults follow the stock
# nerdqaxe_hashrate_benchmark plus the .109 tuning findings. The NerdQAxe++ wants
# more power/voltage/frequency/thermal headroom than a BitAxe.
voltage_increment = 10        # NerdOS voltage options step 10mV
frequency_increment = 20      # NerdOS frequency options step ~15-25MHz
sleep_time = 150              # Settle longer than a BitAxe: 4 ASICs + PID fans ramp slower
benchmark_time = 600          # 10 minutes benchmark time
sample_interval = 15          # 15 seconds sample interval
max_temp = 70                 # NerdQAxe++ overheat cutoff is 70C
max_allowed_voltage = 1300    # Core voltage cap (product range 1.15-1.25V; firmware allows to 1300)
max_allowed_frequency = 1000  # NerdQAxe++ absMaxFrequency
max_vr_temp = 85              # TPS546D24A VR cutoff
min_input_voltage = 11600     # 12V DC input floor (mV)
max_input_voltage = 12500     # 12V DC input ceiling (mV)
max_power = 100               # 24/7 default: 124W PSU * 80% continuous ~= 100W. Override with --max-power.
min_allowed_voltage = 1120    # NerdOS minimum core voltage option
min_allowed_frequency = 500   # NerdOS minimum frequency option

# Warmup samples excluded from temperature and error-rate windows
warmup_samples = 6

# Runtime configuration (populated in main from CLI args)
bitaxe_ip = None
initial_voltage = None
initial_frequency = None
max_error_rate = 3.5          # Error-rate ceiling in percent (legacy; NerdOS has no errorPercentage)
error_gate_enabled = True     # Discard combos above the ceiling when selecting best
min_hashrate_health = 0.95    # NerdOS gate: require avg hashrate >= this fraction of theoretical
health_gate_enabled = True    # Use hashrate-health + dupNonce to gate the best setting
benchmark_mode = "grid"       # "grid" or "refine"
resume_enabled = False
thermal_margin = 1.5          # Require avg chip temp <= max_temp - this to pass (headroom)
vr_margin = 4.0               # Require avg VR temp <= max_vr_temp - this to pass
soak_minutes = 0              # Post-run soak-verify duration (0 = off)
bracket_enabled = False       # refine: also test one frequency step down for J/TH
selection_prefer = 'efficiency'  # winner order among passers: 'efficiency' (lowest J/TH) or 'hashrate'

# Determined from the device
small_core_count = None
asic_count = None
default_voltage = None
default_frequency = None

# Results storage
results = []
results_basename_override = None   # set by --resume to keep writing the same file

# Abort reasons that mean "too hot / too much power": lower frequency, don't add voltage
THERMAL_REASONS = ("CHIP_TEMP_EXCEEDED", "VR_TEMP_EXCEEDED", "POWER_CONSUMPTION_EXCEEDED")

# Printed when a restore request fails, so success is never claimed falsely.
RESTORE_FAIL_WARNING = ("WARNING: could not confirm the restore - the miner may still be on the last "
                        "test settings. Check it and reapply your preferred setting.")

# State flags
handling_interrupt = False
system_reset_done = False
check_mode = False            # read-only --check run: never mutate the device
interrupted = False           # sticky: set on Ctrl+C, never cleared (gates the soak)


def parse_arguments():
    parser = argparse.ArgumentParser(description='NerdQAxe++ Hashrate Benchmark Tool (health-aware)')
    parser.add_argument('bitaxe_ip', nargs='?', help='IP address of the NerdQAxe (e.g., 192.168.2.26)')
    parser.add_argument('-v', '--voltage', type=int, default=None,
                        help='Initial voltage in mV (grid default: 1150; refine/efficiency default: device current)')
    parser.add_argument('-f', '--frequency', type=int, default=None,
                        help='Initial frequency in MHz (grid default: 500; refine/efficiency default: device current)')
    parser.add_argument('--mode', choices=['grid', 'refine', 'efficiency'], default='grid',
                        help="'grid' full sweep (default), 'refine' rescue an unstable ASIC "
                             "(sweep voltage up, drop frequency if thermally capped), or "
                             "'efficiency' trim voltage down on an already-healthy miner")
    parser.add_argument('--voltage-step', type=int, default=None,
                        help='Voltage increment in mV (default: 20)')
    parser.add_argument('--frequency-step', type=int, default=None,
                        help='Frequency increment in MHz (default: 25)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print the resolved plan and exit without touching the device')
    parser.add_argument('--check', action='store_true',
                        help='Read-only: measure the current setting once (no changes, no reboot) and '
                             'report; uses a shorter ~240s window unless --benchmark-time is given')
    parser.add_argument('--max-power', type=int, default=100,
                        help='Power cap in W for selecting the best setting (default: 100 - the 124W '
                             'stock PSU at its 80%% continuous rating; raise it for a bigger PSU)')
    parser.add_argument('--min-health', type=float, default=0.95,
                        help='Hashrate-health floor: require a setting to deliver at least this fraction '
                             'of theoretical hashrate (default: 0.95). A starved undervolt droops below it')
    parser.add_argument('--no-health-gate', action='store_true',
                        help='Report hashrate health but do not use it to gate the best setting')
    parser.add_argument('--max-error', type=float, default=3.5,
                        help='Legacy error ceiling (NerdOS exposes no errorPercentage, so this is inert)')
    parser.add_argument('--max-temp', type=int, default=70,
                        help='Chip temperature cutoff in C (default: 70; NerdQAxe++ overheat limit)')
    parser.add_argument('--thermal-margin', type=float, default=1.5,
                        help='Require a passing combo to average this many C below --max-temp, '
                             'for steady-state headroom (default: 1.5)')
    parser.add_argument('--soak', type=int, default=0,
                        help='After applying the best setting, watch it at steady state for this many '
                             'minutes and warn if temp/error drift over (0 = off; useful for refine)')
    parser.add_argument('--bracket', action='store_true',
                        help='refine: after a passing frequency, also test one step lower for better J/TH')
    parser.add_argument('--prefer', choices=['efficiency', 'hashrate'], default='efficiency',
                        help="Among settings that pass the error/thermal gates, order the winner by "
                             "'efficiency' (lowest J/TH, the default) or 'hashrate' (highest first)")
    parser.add_argument('--no-error-gate', action='store_true',
                        help='Report error rate but do not use it to gate the best setting (legacy pick behavior)')
    parser.add_argument('--resume', action='store_true',
                        help='Reload existing results for this IP and skip already-tested combos')
    parser.add_argument('--benchmark-time', type=int, default=None,
                        help='Override per-combo benchmark window in seconds (default: 600)')

    # If no arguments are provided, print help and exit
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    return parser.parse_args()


# --------------------------------------------------------------------------- #
# Pure helpers (no globals, no I/O) - unit-tested in tests/                    #
# --------------------------------------------------------------------------- #

def sum_error_count(info):
    """Sum the raw ASIC error counts from a /api/system/info payload.

    Returns an int, or None if the field is not present (older firmware)."""
    monitor = info.get("hashrateMonitor") if isinstance(info, dict) else None
    if not monitor:
        return None
    asics = monitor.get("asics")
    if not asics:
        return None
    total = 0
    found = False
    for asic in asics:
        ec = asic.get("errorCount")
        if ec is not None:
            total += ec
            found = True
    return total if found else None


def compute_window_error(samples):
    """ASIC error rate (percent) over a combo's post-warmup samples.

    The device's `errorPercentage` is a noisy rolling rate (not a cumulative
    ratio), so we average it across the stable window, which matches what AxeOS
    reports. With enough samples we drop one extreme at each end (a light trimmed
    mean) so a single spike can't push a borderline combo over the ceiling. Each
    sample is a dict with 'error_percentage' (float|None). Returns
    (rate_or_None, method_string)."""
    eps = [s['error_percentage'] for s in samples if s.get('error_percentage') is not None]
    if not eps:
        return (None, 'unavailable')
    if len(eps) >= 5:
        trimmed = sorted(eps)[1:-1]
        return (sum(trimmed) / len(trimmed), 'errorPercentage-trimmed-mean')
    return (sum(eps) / len(eps), 'errorPercentage-mean')


def best_case_trimmed_error(eps_post, total_post):
    """Lowest the final trimmed-mean error could still reach if every not-yet-taken
    sample read 0%: keep the collected error sum, drop one high (the max) and one
    low (one of those future 0s), over the final trimmed denominator (total_post-2).

    This is only a valid lower bound while at least one sample slot is still open to
    supply the 0 that gets trimmed as the low. At a full window there is no such 0,
    and the real trimmed mean (compute_window_error, which also drops the actual
    min) is lower - so callers must not use this to abort a full window. Returns
    None when there are too few samples to project (<5 collected, or total_post<=2)."""
    if total_post <= 2 or len(eps_post) < 5:
        return None
    return (sum(eps_post) - max(eps_post)) / (total_post - 2)


def stddev(values):
    """Sample standard deviation of a list of numbers (0.0 for fewer than 2)."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return (sum((x - mean) ** 2 for x in values) / (n - 1)) ** 0.5


def window_error_stats(samples):
    """Dispersion of a window's error readings: sample std / min / max / count.

    A large spread flags a suspect measurement (worth re-checking a combo that
    lands right on the ceiling). Returns a dict, or None with no error data."""
    eps = [s['error_percentage'] for s in samples if s.get('error_percentage') is not None]
    if not eps:
        return None
    return {"std": stddev(eps), "min": min(eps), "max": max(eps), "n": len(eps)}


def window_error_count(samples):
    """Raw ASIC hardware errors accrued across the window.

    Because every combo runs the same fixed-length window, this is a directly
    comparable error-volume diagnostic. Summing consecutive positive deltas
    (rather than just endpoints) stays correct even if the counter resets on a
    mid-window reboot. Returns an int, or None if the device does not expose a
    raw error count."""
    counts = [s['error_count'] for s in samples if s.get('error_count') is not None]
    if len(counts) < 2:
        return None
    total = 0
    for prev, cur in zip(counts, counts[1:]):
        if cur >= prev:
            total += cur - prev
        # a decrease means the counter reset; skip that step rather than count it
    return total


def hashrate_health(avg_hashrate, expected_hashrate):
    """Fraction of theoretical hashrate actually delivered (avg / expected).

    NerdOS exposes no errorPercentage, so this ratio is the primary health signal:
    an under-fed (starved) ASIC silently drops effective hashrate while its
    duplicate-nonce counter stays at zero, so measured hashrate falling below
    theoretical is what flags an undervolt at a given frequency. Returns a float,
    or None when expected is unknown/zero (health then never disqualifies)."""
    if not expected_hashrate or expected_hashrate <= 0:
        return None
    return avg_hashrate / expected_hashrate


def passes_health_gate(health, min_health):
    """A combo passes when its hashrate health is unknown (None) or at/above the
    floor. On NerdQAxe this replaces the AxeOS error gate."""
    return health is None or health >= min_health


def dupnonce_delta(samples):
    """Duplicate hardware nonces accrued across the window (NerdOS duplicateHWNonces).

    A secondary hardware-health signal: a stable chip holds this at 0, a rising
    count means it is emitting duplicate/bad work. Sums only positive steps so a
    mid-window reboot's counter reset is not miscounted (same approach as
    window_error_count). Each sample is a dict with 'dup_nonces' (int|None).
    Returns an int, or None if the device never reported the counter."""
    counts = [s['dup_nonces'] for s in samples if s.get('dup_nonces') is not None]
    if len(counts) < 2:
        return None
    total = 0
    for prev, cur in zip(counts, counts[1:]):
        if cur >= prev:
            total += cur - prev
    return total


def _max_present(*values):
    """Max of the non-None values, or None if all are None (for 2-fan telemetry)."""
    present = [v for v in values if v is not None]
    return max(present) if present else None


def passes_error_gate(error_rate, ceiling):
    """A combo passes when its error rate is unknown (None) or within the ceiling."""
    return error_rate is None or error_rate <= ceiling


def passes_thermal(res, max_t, chip_margin, max_vr, vr_margin):
    """Whether a combo leaves thermal headroom at steady state: its average chip
    temp is at least chip_margin below the cap, and its average VR temp is at
    least vr_margin below the VR cap. Missing readings don't disqualify."""
    t = res.get("averageTemperature")
    if t is not None and t > max_t - chip_margin:
        return False
    vr = res.get("averageVRTemp")
    if vr is not None and vr > max_vr - vr_margin:
        return False
    return True


def temp_slope_per_min(temps, interval_s):
    """Least-squares slope of a temperature series, in C per minute (None if too
    few points). A clearly positive slope means the chip was still heating at the
    end of the window, i.e. not thermally settled."""
    n = len(temps)
    if n < 4:
        return None
    mx = (n - 1) / 2.0
    my = sum(temps) / n
    denom = sum((i - mx) ** 2 for i in range(n))
    if denom == 0:
        return 0.0
    slope_per_sample = sum((i - mx) * (temps[i] - my) for i in range(n)) / denom
    return slope_per_sample * (60.0 / interval_s)


def cooling_limited(results, max_t):
    """True when the tested combos show a chip pinned near the temperature cap with
    the fan maxed - degraded cooling that tuning cannot fix. Needs fan data."""
    fanned = [r for r in results if r.get("averageFanSpeed") is not None]
    if not fanned:
        return False
    hot = [r for r in fanned
           if r.get("averageFanSpeed", 0) >= 95 and (r.get("averageTemperature") or 0) >= max_t - 2]
    return bool(hot) and len(hot) >= len(fanned) / 2


def select_best(all_results, ceiling, gate_enabled=True, prefer=None):
    """Pick the best result among the settings that pass the gates.

    With prefer='efficiency' (the default) the winner is the lowest J/TH, ties to
    higher hashrate then steadier hashrate. With prefer='hashrate' the winner is
    the highest hashrate, ties to lower J/TH then steadier hashrate; use this when
    raw output matters more than power, so a leaner-but-slower combo can't win over
    a faster one that also passes (both are already within every gate).

    A passer must stay within the error ceiling, hold its hashrate in tolerance
    (a throttling combo must not win), and leave thermal headroom (avg temp/VR
    below their caps by a margin, so a combo that only passed a short window but
    runs hotter at steady state is not chosen). Falls back to the lowest-error,
    in-tolerance combo when nothing passes. Returns the chosen dict, or None."""
    if prefer is None:
        prefer = selection_prefer
    if not all_results:
        return None

    def in_tolerance(r):
        return r.get('hashrateWithinTolerance', True)

    def gate_ok(r):
        # Recompute from the raw error rate and the active ceiling rather than the
        # persisted passedErrorGate, so a resume with a different --max-error (or a
        # legacy file that predates the field) still decides correctly.
        return passes_error_gate(r.get('errorRate'), ceiling)

    def health_ok(r):
        # NerdOS primary gate: the delivered hashrate must not droop below the floor
        # (a starved undervolt), and the duplicate-nonce counter must not have
        # climbed. Self-gates on health_gate_enabled so it is inert when disabled.
        if not health_gate_enabled:
            return True
        if not passes_health_gate(r.get('hashrateHealth'), min_hashrate_health):
            return False
        return not r.get('dupNonceDelta')   # None or 0 passes; any positive step fails

    def thermal_ok(r):
        return passes_thermal(r, max_temp, thermal_margin, max_vr_temp, vr_margin)

    if gate_enabled:
        passers = [r for r in all_results
                   if gate_ok(r) and health_ok(r) and in_tolerance(r) and thermal_ok(r)]
    else:
        passers = [r for r in all_results if health_ok(r) and in_tolerance(r) and thermal_ok(r)]

    def settled_first(r):
        return not r.get('thermallySettled', True)

    if prefer == 'hashrate':
        def rank(r):
            # Highest hashrate first, then most efficient, then thermally-settled,
            # then steadier hashrate.
            return (-r['averageHashRate'], round(r['efficiencyJTH'], 1),
                    settled_first(r), r.get('hashrateStd') or 0)
    else:
        def rank(r):
            # Bucket J/TH to 0.1 so near-equal-efficiency combos are then ordered by
            # thermally-settled, higher hashrate, and steadier hashrate.
            return (round(r['efficiencyJTH'], 1), settled_first(r),
                    -r['averageHashRate'], r.get('hashrateStd') or 0)

    if passers:
        return sorted(passers, key=rank)[0]

    # Nothing passed - prefer in-tolerance and full-window data, then the least
    # starved (highest hashrate health), then lowest error, then efficiency.
    # (Early-aborted combos are partial-window floors.)
    def err_key(r):
        er = r.get('errorRate')
        return er if er is not None else float('inf')

    def health_key(r):
        h = r.get('hashrateHealth')
        return -h if h is not None else 0   # higher health first

    return sorted(all_results, key=lambda r: (not in_tolerance(r), bool(r.get('earlyAborted')),
                                              health_key(r), err_key(r), r['efficiencyJTH']))[0]


# --------------------------------------------------------------------------- #
# Device I/O                                                                   #
# --------------------------------------------------------------------------- #

def fetch_default_settings():
    """Read the device's CURRENT voltage/frequency (the refine start point and the
    restore-on-exit baseline) plus core/ASIC counts. Current settings come from
    /api/system/info; only missing pieces are backfilled from
    /api/system/asic (whose defaultVoltage/defaultFrequency are STOCK values, not
    the running ones - so they must never overwrite present current settings)."""
    global default_voltage, default_frequency, small_core_count, asic_count

    try:
        response = requests.get(f"{bitaxe_ip}/api/system/info", timeout=10)
        response.raise_for_status()
        system_info = response.json()
    except requests.exceptions.RequestException as e:
        print(RED + f"Error fetching from /api/system/info: {e}" + RESET)
        sys.exit(1)

    if "smallCoreCount" not in system_info:
        print(RED + "Error: smallCoreCount field missing from /api/system/info response." + RESET)
        print(RED + "Cannot proceed without core count information for hashrate calculations." + RESET)
        sys.exit(1)

    small_core_count = system_info.get("smallCoreCount")
    default_voltage = system_info.get("coreVoltage")
    default_frequency = system_info.get("frequency")
    asic_count = system_info.get("asicCount")

    # Backfill only what /info didn't provide from the newer /api/system/asic split.
    if default_voltage is None or default_frequency is None or asic_count is None:
        try:
            asic_info = requests.get(f"{bitaxe_ip}/api/system/asic", timeout=10).json()
            if default_voltage is None:
                default_voltage = asic_info.get("defaultVoltage")
            if default_frequency is None:
                default_frequency = asic_info.get("defaultFrequency")
            if asic_count is None:
                asic_count = asic_info.get("asicCount")
            print(YELLOW + "Backfilled missing fields from /api/system/asic." + RESET)
        except requests.exceptions.RequestException as e:
            print(YELLOW + f"Could not reach /api/system/asic: {e}" + RESET)

    # Final safety fallbacks.
    if default_voltage is None:
        default_voltage = 1150
    if default_frequency is None:
        default_frequency = 500
    if asic_count is None:
        asic_count = 1

    print(GREEN + f"Current settings determined:\n"
                  f"  Core Voltage: {default_voltage}mV\n"
                  f"  Frequency: {default_frequency}MHz\n"
                  f"  ASIC Configuration: {small_core_count * asic_count} total cores" + RESET)


def handle_sigint(signum, frame):
    global system_reset_done, handling_interrupt, interrupted

    interrupted = True   # sticky: never cleared, so post-run steps (soak) can skip
    if handling_interrupt:
        return           # already mid-restore; ignore re-entrant signal
    if system_reset_done:
        sys.exit(0)      # restore already done (e.g. during a soak) - let Ctrl+C exit

    # A --check run never changed anything, so exit without touching the device.
    if check_mode:
        print(RED + "\nHealth check interrupted; no changes made." + RESET)
        sys.exit(0)

    handling_interrupt = True
    print(RED + "Benchmarking interrupted by user." + RESET)

    try:
        if results:
            restored = reset_to_best_setting()
            save_results()
            if restored:
                print(GREEN + "NerdQAxe reset to best or starting settings and results saved." + RESET)
            else:
                print(RED + RESTORE_FAIL_WARNING + RESET)
        else:
            print(YELLOW + "No valid benchmarking results found. Restoring the device's starting settings." + RESET)
            if not set_system_settings(default_voltage, default_frequency):
                print(RED + RESTORE_FAIL_WARNING + RESET)
    finally:
        system_reset_done = True
        handling_interrupt = False
        sys.exit(0)


def get_system_info():
    retries = 5
    for attempt in range(retries):
        try:
            response = requests.get(f"{bitaxe_ip}/api/system/info", timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.Timeout:
            print(YELLOW + f"Timeout while fetching system info. Attempt {attempt + 1} of {retries}." + RESET)
        except requests.exceptions.ConnectionError:
            print(RED + f"Connection error while fetching system info. Attempt {attempt + 1} of {retries}." + RESET)
        except requests.exceptions.RequestException as e:
            print(RED + f"Error fetching system info: {e}" + RESET)
            break
        time.sleep(5)
    return None


def set_system_settings(core_voltage, frequency):
    """Apply settings and restart. Returns True only if both HTTP requests
    succeeded, so callers can report a failed restore instead of assuming it
    worked. The post-apply stabilization wait is handled by
    monitored_stabilization() for combos being benchmarked; restores don't wait
    (nothing is measured after them)."""
    settings = {
        "coreVoltage": core_voltage,
        "frequency": frequency
    }
    try:
        response = requests.patch(f"{bitaxe_ip}/api/system", json=settings, timeout=10)
        response.raise_for_status()
        print(YELLOW + f"Applying settings: Voltage = {core_voltage}mV, Frequency = {frequency}MHz" + RESET)
        time.sleep(2)
        return restart_system()
    except requests.exceptions.RequestException as e:
        print(RED + f"Error setting system settings: {e}" + RESET)
        return False


def restart_system():
    """Restart the device. Returns True on success, False if the request failed."""
    try:
        response = requests.post(f"{bitaxe_ip}/api/system/restart", timeout=10)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(RED + f"Error restarting the system: {e}" + RESET)
        return False


def _quick_info():
    """One quick GET of /api/system/info; None on any error (the device may be
    mid-reboot). Used for lightweight polling that shouldn't spam retries."""
    try:
        r = requests.get(f"{bitaxe_ip}/api/system/info", timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException:
        return None


def monitored_stabilization():
    """Wait up to sleep_time for the device to settle after applying a combo,
    polling for over-limit conditions. Returns a THERMAL_REASONS value if a limit
    is exceeded during the wait - so the caller can treat the combo as thermally
    capped without wasting the whole sample window - or None once the wait
    completes with no violation."""
    print(YELLOW + f"Applying new settings and monitoring for up to {sleep_time}s "
                   f"for system stabilization..." + RESET)
    poll = 5
    waited = 0
    # Require two consecutive over-limit polls before aborting, so a single
    # transient spike during the settle doesn't wrongly mark a combo capped.
    strikes = {"CHIP_TEMP_EXCEEDED": 0, "VR_TEMP_EXCEEDED": 0, "POWER_CONSUMPTION_EXCEEDED": 0}
    while waited < sleep_time:
        time.sleep(poll)
        waited += poll
        info = _quick_info()
        if not info:
            continue  # still rebooting
        temp = info.get("temp")
        vr = info.get("vrTemp")
        power = info.get("power")
        breach = None
        if temp is not None and temp >= max_temp:
            breach = "CHIP_TEMP_EXCEEDED"
        elif vr is not None and vr >= max_vr_temp:
            breach = "VR_TEMP_EXCEEDED"
        elif power is not None and power > max_power:
            breach = "POWER_CONSUMPTION_EXCEEDED"
        for reason in strikes:
            strikes[reason] = strikes[reason] + 1 if reason == breach else 0
        if breach and strikes[breach] >= 2:
            print(RED + f"{breach} sustained during stabilization "
                        f"(temp {temp}, vr {vr}, power {power}); aborting this combo." + RESET)
            return breach
    return None


def apply_settings(core_voltage, frequency):
    """Apply settings, restart, monitor the stabilization window, and confirm the
    device took the settings.

    Returns (True, None) on success, or (False, reason). `reason` is a
    THERMAL_REASONS value when a limit is breached while the device settles (so a
    bad combo is dropped before the sample window is wasted), or 'APPLY_FAILED'
    when a silently-failed PATCH or a watchdog reboot leaves the device on the
    wrong voltage/frequency (which would otherwise mislabel the measurement)."""
    for attempt in range(2):
        if not set_system_settings(core_voltage, frequency):
            continue  # PATCH/restart request failed; retry without wasting a 90s soak
        reason = monitored_stabilization()
        if reason is not None:
            return False, reason
        info = get_system_info()
        if info and info.get("coreVoltage") == core_voltage and info.get("frequency") == frequency:
            return True, None
        got_v = info.get("coreVoltage") if info else "?"
        got_f = info.get("frequency") if info else "?"
        print(YELLOW + f"Applied settings not confirmed (attempt {attempt + 1}/2): requested "
                       f"{core_voltage}mV/{frequency}MHz, device reports {got_v}mV/{got_f}MHz." + RESET)
    return False, "APPLY_FAILED"


def _iter_result(ok=False, reason=None, **kw):
    """Structured return for benchmark_iteration. Returning a dict (not a
    positional tuple) means callers read fields by name, so adding a field can
    never silently shift an unpack and crash a run mid-sweep."""
    r = {
        "ok": ok,
        "reason": reason,
        "averageHashRate": None,
        "averageTemperature": None,
        "efficiencyJTH": None,
        "hashrateWithinTolerance": False,
        "averageVRTemp": None,
        "errorRate": None,
        "errorCountDelta": None,
        "errorStats": None,
        "hashrateStd": None,
        "averageFanSpeed": None,
        "averageFanRpm": None,
        "thermallySettled": True,
        "tempSlope": None,
        "earlyAborted": False,
    }
    r.update(kw)
    return r


def _iter_fail(reason):
    return _iter_result(ok=False, reason=reason)


_no_error_data_warned = False


def _warn_no_error_data():
    """One-time prominent notice that the gate is inert on this device."""
    global _no_error_data_warned
    if not _no_error_data_warned:
        print(YELLOW + "Note: this device exposes no error-rate data (NerdOS) - the legacy error "
                       "gate is inert; stability is judged by hashrate health + duplicate nonces." + RESET)
        _no_error_data_warned = True


def _finalize_window(hash_rates, temperatures, power_consumptions, vr_temps,
                     error_samples, expected_hashrate, fan_speeds=None, fan_rpms=None,
                     early=False):
    """Turn a window's collected samples into a result dict. Called both at
    end-of-window and on an early abort - so a marginal device that never clears
    the ceiling still leaves a recorded (partial) floor for select_best."""
    if not (hash_rates and temperatures and power_consumptions):
        print(YELLOW + "No Hashrate or Temperature or Watts data collected." + RESET)
        return _iter_fail("NO_DATA_COLLECTED")

    # Hashrate: drop 3 high + 3 low as outliers when there is enough data. Its
    # standard deviation over that trimmed set measures how steady the combo is.
    sorted_hashrates = sorted(hash_rates)
    trimmed_hashrates = sorted_hashrates[3:-3] if len(sorted_hashrates) > 6 else sorted_hashrates
    average_hashrate = sum(trimmed_hashrates) / len(trimmed_hashrates)
    hashrate_std = stddev(trimmed_hashrates)

    # Temp/VR/power: drop the warmup samples chronologically (the first N), not by
    # value - clipping the lowest readings anywhere would bias the average high.
    chron_temps = temperatures[warmup_samples:] if len(temperatures) > warmup_samples else temperatures
    average_temperature = sum(chron_temps) / len(chron_temps)

    # Is the chip still heating at the end of the window? A short window can pass
    # just under the cap while true steady state creeps higher.
    temp_slope = temp_slope_per_min(chron_temps, sample_interval)
    thermally_settled = temp_slope is None or temp_slope < 0.15

    average_vr_temp = None
    if vr_temps:
        chron_vr = vr_temps[warmup_samples:] if len(vr_temps) > warmup_samples else vr_temps
        average_vr_temp = sum(chron_vr) / len(chron_vr)

    trimmed_power = power_consumptions[warmup_samples:] if len(power_consumptions) > warmup_samples else power_consumptions
    average_power = sum(trimmed_power) / len(trimmed_power)

    def _avg_post_warmup(values):
        if not values:
            return None
        window = values[warmup_samples:] if len(values) > warmup_samples else values
        return sum(window) / len(window) if window else None

    average_fan_speed = _avg_post_warmup(fan_speeds or [])
    average_fan_rpm = _avg_post_warmup(fan_rpms or [])

    if average_hashrate <= 0:
        print(RED + "Warning: Zero hashrate detected, skipping efficiency calculation" + RESET)
        return _iter_fail("ZERO_HASHRATE")
    efficiency_jth = average_power / (average_hashrate / 1_000)

    error_window = error_samples[warmup_samples:]
    error_rate, error_method = compute_window_error(error_window)
    error_count_delta = window_error_count(error_window)
    error_stats = window_error_stats(error_window)

    # NerdOS health signals (see hashrate_health/dupnonce_delta): the delivered
    # fraction of theoretical hashrate is the primary stability signal, the
    # duplicate-nonce delta the secondary one.
    hashrate_health_ratio = hashrate_health(average_hashrate, expected_hashrate)
    dup_nonce_delta = dupnonce_delta(error_window)

    hashrate_within_tolerance = (average_hashrate >= expected_hashrate * 0.94)

    tag = " [early]" if early else ""
    print(GREEN + f"Average Hashrate{tag}: {average_hashrate:.2f} GH/s (±{hashrate_std:.1f}, "
                  f"Expected: {expected_hashrate:.2f} GH/s)" + RESET)
    heating = "" if thermally_settled else f"  (still heating +{temp_slope:.2f}°C/min, not settled)"
    colour_t = GREEN if thermally_settled else YELLOW
    print(colour_t + f"Average Temperature: {average_temperature:.2f}°C{heating}" + RESET)
    if average_vr_temp is not None:
        print(GREEN + f"Average VR Temperature: {average_vr_temp:.2f}°C" + RESET)
    if average_fan_speed is not None:
        rpm = f" ({average_fan_rpm:.0f} RPM)" if average_fan_rpm is not None else ""
        print(GREEN + f"Average Fan: {average_fan_speed:.0f}%{rpm}" + RESET)
    print(GREEN + f"Efficiency: {efficiency_jth:.2f} J/TH" + RESET)
    if hashrate_health_ratio is not None:
        healthy = passes_health_gate(hashrate_health_ratio, min_hashrate_health)
        dup_note = f", dupNonce +{dup_nonce_delta}" if dup_nonce_delta else ""
        colour_h = GREEN if (healthy and not dup_nonce_delta) else RED
        verdict = "OK" if (healthy and not dup_nonce_delta) else "STARVED/UNSTABLE"
        print(colour_h + f"Hashrate health: {hashrate_health_ratio*100:.1f}% of theoretical{dup_note} "
                         f"[{verdict} @ {min_hashrate_health*100:.0f}%]" + RESET)
    if error_rate is not None:
        gate_note = "PASS" if passes_error_gate(error_rate, max_error_rate) else "OVER CEILING"
        colour = GREEN if gate_note == "PASS" else RED
        extra = f", {error_count_delta} hw errors" if error_count_delta is not None else ""
        spread = f" (±{error_stats['std']:.1f})" if (error_stats and error_stats.get("std") is not None) else ""
        method_label = "trimmed mean" if "trimmed" in error_method else "mean"
        print(colour + f"Error Rate: {error_rate:.2f}%{spread} ({method_label}{extra}) [{gate_note} @ {max_error_rate:.1f}%]" + RESET)
    else:
        _warn_no_error_data()

    return _iter_result(
        ok=True,
        reason=("EARLY_ABORT" if early else None),
        averageHashRate=average_hashrate,
        averageTemperature=average_temperature,
        efficiencyJTH=efficiency_jth,
        hashrateWithinTolerance=hashrate_within_tolerance,
        hashrateHealth=hashrate_health_ratio,
        dupNonceDelta=dup_nonce_delta,
        averageVRTemp=average_vr_temp,
        errorRate=error_rate,
        errorCountDelta=error_count_delta,
        errorStats=error_stats,
        hashrateStd=hashrate_std,
        averageFanSpeed=average_fan_speed,
        averageFanRpm=average_fan_rpm,
        thermallySettled=thermally_settled,
        tempSlope=temp_slope,
        earlyAborted=early,
    )


def benchmark_iteration(core_voltage, frequency):
    current_time = time.strftime("%H:%M:%S")
    print(GREEN + f"[{current_time}] Starting benchmark for Core Voltage: {core_voltage}mV, Frequency: {frequency}MHz" + RESET)
    hash_rates = []
    temperatures = []
    power_consumptions = []
    vr_temps = []
    error_samples = []
    fan_speeds = []
    fan_rpms = []
    total_samples = benchmark_time // sample_interval
    expected_hashrate = frequency * ((small_core_count * asic_count) / 1000)

    for sample in range(total_samples):
        info = get_system_info()
        if info is None:
            print(YELLOW + "Skipping this iteration due to failure in fetching system info." + RESET)
            return _iter_fail("SYSTEM_INFO_FAILURE")

        temp = info.get("temp")
        vr_temp = info.get("vrTemp")
        voltage = info.get("voltage")

        # Right after a reboot the sensors can briefly read null/near-zero. Skip
        # those samples rather than aborting the whole combo over a warmup glitch.
        if temp is None or temp < 5 or voltage is None:
            print(YELLOW + "Sensor warming up (temp/voltage not ready yet); skipping sample." + RESET)
            if sample < total_samples - 1:
                time.sleep(sample_interval)
            continue

        if temp >= max_temp:
            print(RED + f"Chip temperature exceeded {max_temp}°C! Stopping current benchmark." + RESET)
            return _iter_fail("CHIP_TEMP_EXCEEDED")

        if vr_temp is not None and vr_temp >= max_vr_temp:
            print(RED + f"Voltage regulator temperature exceeded {max_vr_temp}°C! Stopping current benchmark." + RESET)
            return _iter_fail("VR_TEMP_EXCEEDED")

        if voltage < min_input_voltage:
            print(RED + f"Input voltage is below the minimum allowed value of {min_input_voltage}mV! Stopping current benchmark." + RESET)
            return _iter_fail("INPUT_VOLTAGE_BELOW_MIN")

        if voltage > max_input_voltage:
            print(RED + f"Input voltage is above the maximum allowed value of {max_input_voltage}mV! Stopping current benchmark." + RESET)
            return _iter_fail("INPUT_VOLTAGE_ABOVE_MAX")

        hash_rate = info.get("hashRate")
        power_consumption = info.get("power")

        if hash_rate is None or power_consumption is None:
            print(YELLOW + "Hashrate or Watts data not ready yet; skipping sample." + RESET)
            if sample < total_samples - 1:
                time.sleep(sample_interval)
            continue

        # Unstable/undervolted chips can report a garbage hashrate far above the
        # theoretical maximum. Skip such samples so they don't pollute the average
        # or the rankings (a combo that only reports garbage ends up NO_DATA; a
        # genuine zero still flows through to the ZERO_HASHRATE path below).
        if expected_hashrate > 0 and hash_rate > expected_hashrate * 2:
            print(YELLOW + f"Implausible hashrate reading ({int(hash_rate)} GH/s vs expected "
                           f"{int(expected_hashrate)}); skipping sample." + RESET)
            if sample < total_samples - 1:
                time.sleep(sample_interval)
            continue

        if power_consumption > max_power:
            print(RED + f"Power consumption exceeded {max_power}W! Stopping current benchmark." + RESET)
            return _iter_fail("POWER_CONSUMPTION_EXCEEDED")

        hash_rates.append(hash_rate)
        temperatures.append(temp)
        power_consumptions.append(power_consumption)
        if vr_temp is not None and vr_temp > 0:
            vr_temps.append(vr_temp)
        error_samples.append({
            "error_count": sum_error_count(info),
            "error_percentage": info.get("errorPercentage"),
            "dup_nonces": info.get("duplicateHWNonces"),
        })
        # NerdQAxe++ has two fans (fanspeed/fanspeed2). The PID drives them together,
        # but track the busier one so "fan pegged" reflects the real cooling load.
        fan_speed = _max_present(info.get("fanspeed"), info.get("fanspeed2"))
        fan_rpm = _max_present(info.get("fanrpm"), info.get("fanrpm2"))
        if fan_speed is not None:
            fan_speeds.append(fan_speed)
        if fan_rpm is not None:
            fan_rpms.append(fan_rpm)

        percentage_progress = ((sample + 1) / total_samples) * 100
        status_line = (
            f"[{sample + 1:2d}/{total_samples:2d}] "
            f"{percentage_progress:5.1f}% | "
            f"CV: {core_voltage:4d}mV | "
            f"F: {frequency:4d}MHz | "
            f"H: {int(hash_rate):4d} GH/s | "
            f"IV: {int(voltage):4d}mV | "
            f"T: {int(temp):2d}°C"
        )
        if vr_temp is not None and vr_temp > 0:
            status_line += f" | VR: {int(vr_temp):2d}°C"
        er_now = info.get("errorPercentage")
        if er_now is not None:
            status_line += f" | E: {er_now:4.1f}%"
        if fan_rpm is not None:
            status_line += f" | Fan: {int(fan_rpm)}rpm"
        print(status_line + RESET)

        # Early abort: if even a perfect (0%) remainder can't pull the window mean
        # under the ceiling, this combo is hopeless. Finalize the partial window
        # anyway so it's recorded as a floor rather than discarded. Only while a
        # sample slot is still open (sample < last) - the projection assumes a
        # future 0 to trim as the low, which a full window does not have; there the
        # loop falls through to the real trimmed mean, which decides on its own.
        if error_gate_enabled and sample < total_samples - 1:
            post = error_samples[warmup_samples:]
            eps_post = [s['error_percentage'] for s in post if s['error_percentage'] is not None]
            total_post = total_samples - warmup_samples
            best_case_mean = best_case_trimmed_error(eps_post, total_post)
            if best_case_mean is not None and best_case_mean > max_error_rate:
                print(RED + f"Error ceiling unreachable (best-case trimmed mean {best_case_mean:.1f}% > "
                            f"{max_error_rate:.1f}%); aborting combo early." + RESET)
                return _finalize_window(hash_rates, temperatures, power_consumptions,
                                        vr_temps, error_samples, expected_hashrate,
                                        fan_speeds, fan_rpms, early=True)

        if sample < total_samples - 1:
            time.sleep(sample_interval)

    return _finalize_window(hash_rates, temperatures, power_consumptions,
                            vr_temps, error_samples, expected_hashrate,
                            fan_speeds, fan_rpms, early=False)


def record_result(core_voltage, frequency, avg_hashrate, avg_temp, efficiency_jth,
                  avg_vr_temp, error_rate, error_count_delta=None, hashrate_ok=True,
                  error_stats=None, early_aborted=False, hashrate_std=None,
                  fan_speed=None, fan_rpm=None, thermally_settled=True, temp_slope=None,
                  hashrate_health=None, dup_nonce_delta=None):
    result = {
        "coreVoltage": core_voltage,
        "frequency": frequency,
        "averageHashRate": avg_hashrate,
        "hashrateStd": hashrate_std,
        "averageTemperature": avg_temp,
        "efficiencyJTH": efficiency_jth,
        "errorRate": error_rate,
        "errorCountDelta": error_count_delta,
        "passedErrorGate": passes_error_gate(error_rate, max_error_rate),
        "hashrateWithinTolerance": hashrate_ok,
        "hashrateHealth": hashrate_health,
        "dupNonceDelta": dup_nonce_delta,
        "thermallySettled": thermally_settled,
        "tempSlope": temp_slope,
        "earlyAborted": early_aborted,
    }
    if error_stats is not None:
        result["errorRateStd"] = error_stats.get("std")
        result["errorRateMin"] = error_stats.get("min")
        result["errorRateMax"] = error_stats.get("max")
    if avg_vr_temp is not None:
        result["averageVRTemp"] = avg_vr_temp
    if fan_speed is not None:
        result["averageFanSpeed"] = fan_speed
    if fan_rpm is not None:
        result["averageFanRpm"] = fan_rpm
    results.append(result)
    return result


def already_tested(core_voltage, frequency):
    return _recorded(core_voltage, frequency) is not None


def _recorded(core_voltage, frequency):
    """Return the recorded result for a combo, or None if it hasn't been run."""
    return next((r for r in results if r["coreVoltage"] == core_voltage
                 and r["frequency"] == frequency), None)


def results_filename(ext):
    if results_basename_override:
        return f"{results_basename_override}.{ext}"
    ip_address = bitaxe_ip.replace('http://', '').replace(':', '_')
    return f"nerdqaxe_benchmark_results_{ip_address}_{START_TIME}.{ext}"


def load_existing_results():
    """For --resume: reload the most recent prior results file for this IP and keep
    writing to it. Globbing the timestamp (rather than assuming the current hour)
    means a resume that crosses an hour boundary still finds and continues the run."""
    global results, results_basename_override
    ip_address = bitaxe_ip.replace('http://', '').replace(':', '_')
    matches = sorted(glob.glob(f"nerdqaxe_benchmark_results_{ip_address}_*.json"))
    if not matches:
        print(YELLOW + "Resume: no prior results file found for this IP; starting fresh." + RESET)
        return
    latest = matches[-1]  # %Y-%m-%d_%H%M%S timestamps sort chronologically
    try:
        with open(latest, "r") as f:
            data = json.load(f)
    except (IOError, ValueError):
        print(YELLOW + f"Resume: could not read {latest}; starting fresh." + RESET)
        return
    prior = data.get("all_results", data) if isinstance(data, dict) else data
    if isinstance(prior, list):
        results = prior
        results_basename_override = latest[:-5]  # strip '.json' - keep writing this file
        print(GREEN + f"Resume: loaded {len(results)} prior result(s) from {latest}" + RESET)


def save_results():
    try:
        filename = results_filename("json")
        with open(filename, "w") as f:
            json.dump(results, f, indent=4)
        print(GREEN + f"Results saved to {filename}" + RESET)
        print()
    except IOError as e:
        print(RED + f"Error saving results to file: {e}" + RESET)


def save_csv():
    if not results:
        return
    try:
        filename = results_filename("csv")
        fields = ["coreVoltage", "frequency", "averageHashRate", "hashrateStd", "efficiencyJTH",
                  "hashrateHealth", "dupNonceDelta", "errorRate", "errorRateStd", "errorCountDelta",
                  "passedErrorGate", "hashrateWithinTolerance", "thermallySettled", "tempSlope",
                  "earlyAborted", "averageTemperature", "averageVRTemp", "averageFanSpeed", "averageFanRpm"]
        with open(filename, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in results:
                writer.writerow({k: r.get(k, "") for k in fields})
        print(GREEN + f"CSV saved to {filename}" + RESET)
    except IOError as e:
        print(RED + f"Error saving CSV to file: {e}" + RESET)


def reset_to_best_setting():
    """Apply the best (or starting) setting. Returns True only if the apply/restart
    requests succeeded, so the caller doesn't claim success on a failed restore."""
    best_result = select_best(results, max_error_rate, error_gate_enabled)
    if best_result is None:
        print(YELLOW + "No valid benchmarking results found. Restoring the device's starting settings." + RESET)
        return set_system_settings(default_voltage, default_frequency)

    best_voltage = best_result["coreVoltage"]
    best_frequency = best_result["frequency"]
    er = best_result.get("errorRate")
    er_str = f"{er:.2f}%" if er is not None else "n/a"
    if not combo_passes(best_result):
        why = []
        if error_gate_enabled and not passes_error_gate(er, max_error_rate):
            why.append("error ceiling")
        if not best_result.get("hashrateWithinTolerance", True):
            why.append("hashrate tolerance")
        if not passes_thermal(best_result, max_temp, thermal_margin, max_vr_temp, vr_margin):
            why.append("thermal headroom")
        print(YELLOW + f"Warning: no setting met all limits ({', '.join(why)}); "
                       f"applying the least-bad result found." + RESET)
    print(GREEN + f"Applying the best settings from benchmarking:\n"
                  f"  Core Voltage: {best_voltage}mV\n"
                  f"  Frequency: {best_frequency}MHz\n"
                  f"  Efficiency: {best_result['efficiencyJTH']:.2f} J/TH | Error: {er_str}" + RESET)
    # set_system_settings already restarts; no extra reboot needed here.
    return set_system_settings(best_voltage, best_frequency)


def _fmt_row(r):
    er = r.get("errorRate")
    er_str = f"{er:5.2f}%" if er is not None else "  n/a "
    gate = "ok " if r.get("passedErrorGate") else "OVER"
    vr = r.get("averageVRTemp")
    vr_str = f"{vr:4.1f}" if vr is not None else "  - "
    return (f"  {r['coreVoltage']:4d}mV  {r['frequency']:4d}MHz  "
            f"{r['averageHashRate']:7.1f}GH  {r['efficiencyJTH']:5.2f}J/TH  "
            f"err {er_str} [{gate}]  T {r['averageTemperature']:4.1f}  VR {vr_str}")


def print_summary():
    if not results:
        print(RED + "No valid results were found during benchmarking." + RESET)
        return

    top_hash = sorted(results, key=lambda x: x["averageHashRate"], reverse=True)[:5]
    top_eff = sorted(results, key=lambda x: x["efficiencyJTH"])[:5]
    top_lowerr = sorted(results, key=lambda x: (x["errorRate"] if x.get("errorRate") is not None else float('inf')))[:5]

    print(GREEN + "\nTop 5 Highest Hashrate:" + RESET)
    for r in top_hash:
        print(_fmt_row(r))
    print(GREEN + "\nTop 5 Most Efficient (J/TH):" + RESET)
    for r in top_eff:
        print(_fmt_row(r))
    print(GREEN + "\nTop 5 Lowest Error Rate:" + RESET)
    for r in top_lowerr:
        print(_fmt_row(r))

    best = select_best(results, max_error_rate, error_gate_enabled)
    if best is not None:
        er = best.get("errorRate")
        er_str = f"{er:.2f}%" if er is not None else "n/a"
        print(GREEN + f"\nSelected best (error-gate {max_error_rate:.1f}% -> efficiency):" + RESET)
        print(GREEN + f"  {best['coreVoltage']}mV / {best['frequency']}MHz | "
                      f"{best['averageHashRate']:.1f} GH/s | {best['efficiencyJTH']:.2f} J/TH | error {er_str}" + RESET)
    print_cooling_verdict(results)


def print_cooling_verdict(results):
    """Warn when the data shows degraded cooling that tuning cannot fix."""
    if cooling_limited(results, max_temp):
        print(RED + "\nCooling-limited hardware: the fan is maxed and the chip sits near the "
                    "temperature cap even at the tested settings. This is degraded cooling (thermal "
                    "paste, fan, or airflow) - tuning can't fix it. Service the cooling and re-run." + RESET)


def save_final_json():
    """Preserve the upstream all_results / top_performers / most_efficient JSON shape."""
    def slim(r, rank):
        out = {
            "rank": rank,
            "coreVoltage": r["coreVoltage"],
            "frequency": r["frequency"],
            "averageHashRate": r["averageHashRate"],
            "hashrateStd": r.get("hashrateStd"),
            "averageTemperature": r["averageTemperature"],
            "averageFanSpeed": r.get("averageFanSpeed"),
            "averageFanRpm": r.get("averageFanRpm"),
            "efficiencyJTH": r["efficiencyJTH"],
            "hashrateHealth": r.get("hashrateHealth"),
            "dupNonceDelta": r.get("dupNonceDelta"),
            "errorRate": r.get("errorRate"),
            "errorRateStd": r.get("errorRateStd"),
            "errorCountDelta": r.get("errorCountDelta"),
            "passedErrorGate": r.get("passedErrorGate"),
            "hashrateWithinTolerance": r.get("hashrateWithinTolerance"),
            "thermallySettled": r.get("thermallySettled", True),
            "tempSlope": r.get("tempSlope"),
            "earlyAborted": r.get("earlyAborted", False),
        }
        if "averageVRTemp" in r:
            out["averageVRTemp"] = r["averageVRTemp"]
        return out

    top_hash = sorted(results, key=lambda x: x["averageHashRate"], reverse=True)[:5]
    top_eff = sorted(results, key=lambda x: x["efficiencyJTH"])[:5]
    top_lowerr = sorted(results, key=lambda x: (x["errorRate"] if x.get("errorRate") is not None else float('inf')))[:5]

    final_data = {
        "all_results": results,
        "top_performers": [slim(r, i) for i, r in enumerate(top_hash, 1)],
        "most_efficient": [slim(r, i) for i, r in enumerate(top_eff, 1)],
        "lowest_error": [slim(r, i) for i, r in enumerate(top_lowerr, 1)],
    }
    try:
        with open(results_filename("json"), "w") as f:
            json.dump(final_data, f, indent=4)
    except IOError as e:
        print(RED + f"Error saving final JSON: {e}" + RESET)


# --------------------------------------------------------------------------- #
# Benchmark modes                                                             #
# --------------------------------------------------------------------------- #

def run_combo(voltage, frequency):
    """Apply + verify settings, run one benchmark window, and record the result.

    Retries once on a transient info-fetch failure (network blip) so a momentary
    hiccup doesn't end a multi-hour sweep. Returns (recorded_result_or_None,
    error_reason). Records the result (with hashrate tolerance) on success."""
    for attempt in range(2):
        applied, apply_reason = apply_settings(voltage, frequency)
        if not applied:
            # apply_reason may be a THERMAL_REASONS value (over-limit during the
            # stabilization wait), which the caller treats like a thermal cap.
            return None, apply_reason or "APPLY_FAILED"
        r = benchmark_iteration(voltage, frequency)
        if r["ok"]:
            res = record_result(voltage, frequency, r["averageHashRate"], r["averageTemperature"],
                                r["efficiencyJTH"], r["averageVRTemp"], r["errorRate"],
                                r["errorCountDelta"], r["hashrateWithinTolerance"],
                                error_stats=r["errorStats"], early_aborted=r["earlyAborted"],
                                hashrate_std=r["hashrateStd"], fan_speed=r["averageFanSpeed"],
                                fan_rpm=r["averageFanRpm"], thermally_settled=r["thermallySettled"],
                                temp_slope=r["tempSlope"], hashrate_health=r["hashrateHealth"],
                                dup_nonce_delta=r["dupNonceDelta"])
            save_results()
            return res, r["reason"]
        if r["reason"] == "SYSTEM_INFO_FAILURE" and attempt == 0:
            print(YELLOW + "Network hiccup during combo; retrying once." + RESET)
            continue
        return None, r["reason"]
    return None, "SYSTEM_INFO_FAILURE"


def error_and_hashrate_ok(res):
    """The stability gate, ignoring the thermal margin: the (legacy) error gate, the
    NerdOS hashrate-health + duplicate-nonce gate, and hashrate tolerance. A starved
    combo fails here, so refine responds by raising voltage to feed the chip - the
    NerdOS equivalent of clearing an error rate."""
    gate_ok = (not error_gate_enabled) or passes_error_gate(res.get("errorRate"), max_error_rate)
    health_ok = (not health_gate_enabled) or (
        passes_health_gate(res.get("hashrateHealth"), min_hashrate_health)
        and not res.get("dupNonceDelta"))
    return gate_ok and health_ok and res.get("hashrateWithinTolerance", True)


def combo_passes(res):
    # Recompute against the active ceiling (see select_best) rather than trusting a
    # persisted passedErrorGate, which may be stale on a resume. Also require
    # thermal headroom so refine/efficiency don't accept a setting that only passed
    # a short window but runs hotter at steady state.
    return (error_and_hashrate_ok(res)
            and passes_thermal(res, max_temp, thermal_margin, max_vr_temp, vr_margin))


def run_grid():
    """Full voltage/frequency sweep, error-aware."""
    current_voltage = initial_voltage
    current_frequency = initial_frequency

    while current_voltage <= max_allowed_voltage and current_frequency <= max_allowed_frequency:
        if resume_enabled and already_tested(current_voltage, current_frequency):
            rec = _recorded(current_voltage, current_frequency)
            print(YELLOW + f"Resume: skipping already-tested {current_voltage}mV / {current_frequency}MHz" + RESET)
            # Replay the same branch the live run would have taken from this combo.
            if rec is not None and not combo_passes(rec):
                if current_voltage + voltage_increment <= max_allowed_voltage:
                    current_voltage += voltage_increment
                    current_frequency -= frequency_increment
                else:
                    break
            else:
                current_frequency += frequency_increment
            continue

        res, reason = run_combo(current_voltage, current_frequency)

        if res is not None:
            if combo_passes(res):
                # Stable and within the error ceiling: try a higher frequency.
                if current_frequency + frequency_increment <= max_allowed_frequency:
                    current_frequency += frequency_increment
                else:
                    break
            else:
                # Unstable or over the error ceiling: step frequency back, add voltage.
                if current_voltage + voltage_increment <= max_allowed_voltage:
                    current_voltage += voltage_increment
                    current_frequency -= frequency_increment
                    why = "hashrate" if not res.get("hashrateWithinTolerance", True) else "error rate"
                    print(YELLOW + f"{why} out of range. Decreasing frequency to {current_frequency}MHz "
                                   f"and increasing voltage to {current_voltage}mV" + RESET)
                else:
                    break
        elif reason in THERMAL_REASONS:
            # Thermally capped. Lower frequencies at this voltage were already tested
            # on the way up, and adding voltage here would only add heat, so stop.
            # Use --mode refine to push a thermally-limited chip.
            print(GREEN + f"Thermally capped ({reason}) at {current_voltage}mV / {current_frequency}MHz; "
                          f"stopping the sweep. Use --mode refine for a thermally-limited chip." + RESET)
            break
        else:
            print(GREEN + f"Stopping further testing ({reason or 'stability limit'})." + RESET)
            break


def _refine_probe_down(frequency, start_voltage):
    """The starting voltage already passed; probe lower voltages for a leaner
    (better J/TH) setting that still clears the ceiling. select_best picks the
    winner from all recorded passers, so we just need to test them."""
    voltage = start_voltage - voltage_increment
    while voltage >= min_allowed_voltage:
        if resume_enabled and already_tested(voltage, frequency):
            rec = _recorded(voltage, frequency)
            if rec is not None and not combo_passes(rec):
                return  # known failure here - the lowest passer is above this
            voltage -= voltage_increment
            continue
        res, reason = run_combo(voltage, frequency)
        if res is None:
            return  # thermal/limit/blip while probing down - stop
        if combo_passes(res):
            print(GREEN + f"{voltage}mV also clears the ceiling - trying lower for efficiency." + RESET)
            voltage -= voltage_increment
        else:
            print(YELLOW + f"{voltage}mV no longer clears the ceiling; "
                           f"lowest passer is {voltage + voltage_increment}mV." + RESET)
            return


def _refine_sweep_at(frequency):
    """Sweep voltage upward at one frequency. Returns:
      'passed'    - found an in-tolerance setting under the error ceiling
      'capped'    - hit a thermal/power limit (caller should drop frequency)
      'exhausted' - ran out of voltage headroom or couldn't proceed"""
    voltage = initial_voltage
    first = True
    while min_allowed_voltage <= voltage <= max_allowed_voltage:
        if resume_enabled and already_tested(voltage, frequency):
            rec = _recorded(voltage, frequency)
            print(YELLOW + f"Resume: skipping already-tested {voltage}mV / {frequency}MHz" + RESET)
            # Replay the recorded outcome: a known passer ends this frequency;
            # a known failure means keep climbing voltage.
            if rec is not None and combo_passes(rec):
                if first:
                    _refine_probe_down(frequency, voltage)
                return "passed"
            # A recorded combo that's fine on error/hashrate but only lacks thermal
            # headroom: dropping frequency helps, not more voltage.
            if rec is not None and error_and_hashrate_ok(rec):
                return "capped"
            voltage += voltage_increment
            first = False
            continue

        res, reason = run_combo(voltage, frequency)

        if res is not None:
            if combo_passes(res):
                print(GREEN + f"Found stable low-error setting at {voltage}mV / {frequency}MHz." + RESET)
                if first:
                    _refine_probe_down(frequency, voltage)
                return "passed"
            if error_and_hashrate_ok(res):
                # Error and hashrate are fine; it only lacks thermal headroom.
                # Adding voltage would add heat, so lower the frequency instead.
                print(YELLOW + f"{voltage}mV/{frequency}MHz is within error/hashrate but runs too "
                               f"warm; treating as thermally capped." + RESET)
                return "capped"
            # Over ceiling / low hashrate (incl. an early-aborted partial window):
            # needs more voltage.
            voltage += voltage_increment
            first = False
            print(YELLOW + f"Raising voltage to {voltage}mV to reduce error / stabilize." + RESET)
        elif reason in THERMAL_REASONS:
            return "capped"
        else:
            return "exhausted"

    return "exhausted"


def run_refine():
    """Stabilize an unstable ASIC: hold a frequency, sweep voltage up to the lowest
    setting that clears the error ceiling (then probe down for efficiency). If the
    chip hits the temperature ceiling before the error clears, drop the frequency
    and try again, since lowering frequency (not adding voltage) is what helps once
    cooling is the limit."""
    frequency = initial_frequency
    print(GREEN + f"Refine mode: starting at {frequency}MHz, sweeping voltage from "
                  f"{initial_voltage}mV (health floor {min_hashrate_health*100:.0f}%, "
                  f"{max_power}W / {max_temp}°C caps)." + RESET)

    consecutive_caps = 0
    while frequency >= min_allowed_frequency:
        outcome = _refine_sweep_at(frequency)
        if outcome == "passed":
            # --bracket: the highest passing frequency maximizes hashrate, but one
            # step down may be more efficient. Test it and let select_best compare.
            lower = frequency - frequency_increment
            if bracket_enabled and lower >= min_allowed_frequency:
                print(GREEN + f"Bracket: also testing {lower}MHz for better J/TH." + RESET)
                _refine_sweep_at(lower)
            return
        if outcome == "capped":
            consecutive_caps += 1
            # After a few frequencies capped in a row with the fan pegged, this is
            # degraded cooling, not an overclock - don't walk it down to the floor.
            if consecutive_caps >= 3:
                info = _quick_info()
                fan = _max_present(info.get("fanspeed"), info.get("fanspeed2")) if info else None
                if fan is not None and fan >= 95:
                    print(RED + "Cooling-limited hardware: capped at several frequencies with the fans "
                                "maxed. Stopping - service the cooling (paste/fan/airflow); tuning "
                                "can't fix this." + RESET)
                    return
            frequency -= frequency_increment
            if frequency >= min_allowed_frequency:
                print(YELLOW + f"Capped at this frequency (power/thermal); dropping to {frequency}MHz "
                               f"(lowering frequency is what helps once power or cooling is the limit)." + RESET)
            continue
        # exhausted - no more voltage headroom, or could not proceed
        print(GREEN + "Reached stability limits without clearing the ceiling. Stopping." + RESET)
        return

    print(GREEN + "Reached the frequency floor without clearing the ceiling. Stopping." + RESET)


def run_efficiency():
    """For an already-healthy miner: hold the frequency and trim voltage DOWN from
    the current setting to the leanest voltage that still clears the error ceiling.
    Refine rescues; this squeezes J/TH out of a miner that already passes."""
    frequency = initial_frequency
    voltage = initial_voltage
    print(GREEN + f"Efficiency mode: {frequency}MHz fixed, trimming voltage down from "
                  f"{voltage}mV while staying under {max_error_rate:.1f}% error." + RESET)

    if resume_enabled and already_tested(voltage, frequency):
        res = _recorded(voltage, frequency)
    else:
        res, reason = run_combo(voltage, frequency)
        if res is None:
            print(YELLOW + f"Could not benchmark the starting setting ({reason}); aborting efficiency run." + RESET)
            return
    if res is None or not combo_passes(res):
        print(YELLOW + "Starting setting does not pass the error gate - run '--mode refine' "
                       "to stabilize it first, then efficiency." + RESET)
        return

    voltage -= voltage_increment
    while voltage >= min_allowed_voltage:
        if resume_enabled and already_tested(voltage, frequency):
            rec = _recorded(voltage, frequency)
            if rec is not None and not combo_passes(rec):
                break  # recorded failure here; the leanest passer is above this
            voltage -= voltage_increment
            continue
        res, _ = run_combo(voltage, frequency)
        if res is None:
            break
        if combo_passes(res):
            print(GREEN + f"{voltage}mV still clears the ceiling - trying lower for efficiency." + RESET)
            voltage -= voltage_increment
        else:
            print(YELLOW + f"{voltage}mV drops below the ceiling; leanest passer is "
                           f"{voltage + voltage_increment}mV." + RESET)
            break


def run_check():
    """Read-only health snapshot: measure the CURRENT setting over one window
    without changing voltage/frequency or rebooting. Nothing is applied or
    restored - the device keeps running exactly as it was."""
    voltage, frequency = default_voltage, default_frequency
    print(GREEN + f"Health check: measuring the current setting {voltage}mV / {frequency}MHz "
                  f"over ~{benchmark_time}s - no changes, no reboot." + RESET)
    r = benchmark_iteration(voltage, frequency)
    if not r["ok"]:
        print(RED + f"Check could not complete ({r['reason']})." + RESET)
        return False
    er = f"{r['errorRate']:.2f}%" if r["errorRate"] is not None else "n/a"
    gate = ("PASS" if passes_error_gate(r["errorRate"], max_error_rate) else "OVER CEILING") \
        if r["errorRate"] is not None else "no error data"
    vr = f"   VR: {r['averageVRTemp']:.1f}°C" if r["averageVRTemp"] is not None else ""
    partial = "   (partial window - error cut it short)" if r["earlyAborted"] else ""
    hstd = f" (±{r['hashrateStd']:.1f})" if r["hashrateStd"] is not None else ""
    fan = ""
    if r["averageFanSpeed"] is not None:
        rpm = f", {r['averageFanRpm']:.0f} RPM" if r["averageFanRpm"] is not None else ""
        fan = f"\n  Fan:        {r['averageFanSpeed']:.0f}%{rpm}"
    settled = "" if r.get("thermallySettled", True) else "  (still heating - not thermally settled)"
    print(GREEN + f"\nCurrent setting: {voltage}mV / {frequency}MHz{partial}\n"
                  f"  Hashrate:   {r['averageHashRate']:.1f} GH/s{hstd}\n"
                  f"  Efficiency: {r['efficiencyJTH']:.2f} J/TH\n"
                  f"  Error:      {er} [{gate} @ {max_error_rate:.1f}%]\n"
                  f"  Temp:       {r['averageTemperature']:.1f}°C{vr}{settled}{fan}" + RESET)
    print_cooling_verdict([r])
    return True


def run_soak(minutes):
    """After the best setting is applied and the device is running it, watch it at
    steady state (no reboot) and warn if temperature or error drift over their
    limits. This is what turns refine's 'it's fixed now' into a measured claim
    rather than an extrapolation from a short window."""
    info = get_system_info()
    if not info:
        print(YELLOW + "Soak: device not reachable; skipping." + RESET)
        return
    v, f = info.get("coreVoltage"), info.get("frequency")
    print(GREEN + f"\nSoak verify: watching {v}mV / {f}MHz for {minutes} min at steady state "
                  f"(no reboot)..." + RESET)
    poll = 30
    polls = max(1, minutes * 60 // poll)
    max_temp_seen = max_vr_seen = 0.0
    errs = []
    for i in range(polls):
        time.sleep(poll)
        info = _quick_info()
        if not info:
            continue
        t = info.get("temp") or 0
        vr = info.get("vrTemp") or 0
        ep = info.get("errorPercentage")
        max_temp_seen = max(max_temp_seen, t)
        max_vr_seen = max(max_vr_seen, vr)
        if ep is not None:
            errs.append(ep)
        elapsed = (i + 1) * poll
        er_txt = "n/a" if ep is None else f"{ep:.1f}%"
        print(YELLOW + f"  soak {elapsed // 60}m{elapsed % 60:02d}s: {t:.1f}°C  vr {vr:.0f}°C  err {er_txt}" + RESET)
    mean_err = sum(errs) / len(errs) if errs else None
    issues = []
    if max_temp_seen >= max_temp:
        issues.append(f"chip reached {max_temp_seen:.1f}°C (cap {max_temp})")
    if max_vr_seen >= max_vr_temp:
        issues.append(f"VR reached {max_vr_seen:.1f}°C (cap {max_vr_temp})")
    if mean_err is not None and mean_err > max_error_rate:
        issues.append(f"error {mean_err:.1f}% over the {max_error_rate}% ceiling")
    if issues:
        print(RED + "Soak WARNING: the applied setting drifts at steady state - " + "; ".join(issues) +
                    ". Consider a lower frequency, or more headroom via --thermal-margin / --max-temp." + RESET)
    else:
        err_txt = "n/a" if mean_err is None else f"{mean_err:.1f}%"
        print(GREEN + f"Soak OK: steady at max {max_temp_seen:.1f}°C / VR {max_vr_seen:.0f}°C, "
                      f"error {err_txt}." + RESET)


def print_run_expectations():
    """Set expectations before a sweep: reboots, mining downtime, rough duration."""
    per_combo_min = (sleep_time + benchmark_time) / 60.0
    ranges = {
        "grid": "many combos - often 30 min to a few hours",
        "refine": f"a handful of combos (more if it must drop frequency) - roughly {per_combo_min*3:.0f}-{per_combo_min*12:.0f} min",
        "efficiency": f"a few combos - roughly {per_combo_min*2:.0f}-{per_combo_min*6:.0f} min",
    }
    print(YELLOW + "Before you start:" + RESET)
    print(f"  - Each combo reboots the miner and runs ~{per_combo_min:.0f} min; mining is interrupted for the whole run.")
    print(f"  - This '{benchmark_mode}' run tests {ranges.get(benchmark_mode, 'several combos')}.")
    print("  - You can stop anytime with Ctrl+C - it restores the best setting found so far.\n")


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def main():
    global bitaxe_ip, initial_voltage, initial_frequency, benchmark_time
    global max_error_rate, error_gate_enabled, benchmark_mode, resume_enabled
    global max_temp, system_reset_done, voltage_increment, frequency_increment, check_mode
    global thermal_margin, soak_minutes, bracket_enabled, selection_prefer
    global max_power, min_hashrate_health, health_gate_enabled

    # On Windows, this call turns on ANSI escape processing so the colored output
    # renders in the classic console (Windows Terminal already supports it).
    if os.name == "nt":
        os.system("")

    args = parse_arguments()
    if not args.bitaxe_ip:
        print(RED + "Error: NerdQAxe IP address is required." + RESET)
        sys.exit(1)

    check_mode = args.check
    bitaxe_ip = f"http://{args.bitaxe_ip}"
    max_error_rate = args.max_error
    max_temp = args.max_temp
    max_power = args.max_power
    min_hashrate_health = args.min_health
    health_gate_enabled = not args.no_health_gate
    error_gate_enabled = not args.no_error_gate
    benchmark_mode = args.mode
    resume_enabled = args.resume
    thermal_margin = args.thermal_margin
    soak_minutes = args.soak
    bracket_enabled = args.bracket
    selection_prefer = args.prefer
    if args.benchmark_time is not None:
        benchmark_time = args.benchmark_time
    elif args.check:
        benchmark_time = 240   # a health check is quicker than a full 10-min window
    if args.voltage_step is not None:
        voltage_increment = args.voltage_step
    if args.frequency_step is not None:
        frequency_increment = args.frequency_step
    if voltage_increment <= 0 or frequency_increment <= 0:
        raise ValueError(RED + "Error: --voltage-step and --frequency-step must be positive." + RESET)

    total_samples = benchmark_time // sample_interval
    if total_samples - warmup_samples < 8:
        min_time = (warmup_samples + 8) * sample_interval
        raise ValueError(RED + f"Error: Benchmark time too short - only {max(0, total_samples - warmup_samples)} "
                               f"post-warmup samples (need >= 8 for a stable error mean). "
                               f"Use --benchmark-time >= {min_time}." + RESET)

    signal.signal(signal.SIGINT, handle_sigint)

    fetch_default_settings()

    # Read-only health check: measure the current setting and exit, no sweep.
    if args.check:
        if not run_check():
            sys.exit(1)
        return

    # Resolve start voltage/frequency. Grid keeps the upstream 1150/500 start;
    # refine and efficiency start from the device's current settings unless overridden.
    if benchmark_mode in ("refine", "efficiency"):
        initial_frequency = args.frequency if args.frequency is not None else default_frequency
        initial_voltage = args.voltage if args.voltage is not None else default_voltage
    else:
        initial_frequency = args.frequency if args.frequency is not None else 500
        initial_voltage = args.voltage if args.voltage is not None else 1150

    if not (min_allowed_voltage <= initial_voltage <= max_allowed_voltage):
        raise ValueError(RED + f"Error: Initial voltage {initial_voltage}mV outside allowed range "
                               f"{min_allowed_voltage}-{max_allowed_voltage}mV." + RESET)
    if not (min_allowed_frequency <= initial_frequency <= max_allowed_frequency):
        raise ValueError(RED + f"Error: Initial frequency {initial_frequency}MHz outside allowed range "
                               f"{min_allowed_frequency}-{max_allowed_frequency}MHz." + RESET)

    if args.dry_run:
        print(GREEN + "Dry run - resolved plan (no device changes will be made):" + RESET)
        print(f"  Device:     {bitaxe_ip}")
        print(f"  Mode:       {benchmark_mode}")
        print(f"  Start:      {initial_voltage}mV / {initial_frequency}MHz")
        print(f"  Steps:      {voltage_increment}mV voltage, {frequency_increment}MHz frequency")
        print(f"  Ceilings:   {max_error_rate:.1f}% error, {max_temp}°C chip")
        print(f"  Window:     {benchmark_time}s ({benchmark_time // sample_interval} samples, "
              f"{warmup_samples} warmup)")
        print(f"  Error gate: {'on' if error_gate_enabled else 'off'}")
        return

    if resume_enabled:
        load_existing_results()

    print(RED + "\nDISCLAIMER:" + RESET)
    print("This tool will stress test your NerdQAxe by running it at various voltages and frequencies.")
    print("While safeguards are in place, running hardware outside of standard parameters carries inherent risks.")
    print("Use this tool at your own risk. The author(s) are not responsible for any damage to your hardware.")
    print("\nNOTE: Ambient temperature significantly affects these results. The optimal settings found may not")
    print("work well if room temperature changes substantially. Re-run the benchmark if conditions change.\n")

    print_run_expectations()

    exit_code = 0
    try:
        if benchmark_mode == "refine":
            run_refine()
        elif benchmark_mode == "efficiency":
            run_efficiency()
        else:
            run_grid()
    except Exception as e:
        # Let the finally block own restoration so the device is only reset once.
        print(RED + f"An unexpected error occurred: {e}" + RESET)
        exit_code = 1
    finally:
        if not system_reset_done:
            if results:
                restored = reset_to_best_setting()
                save_results()
                if restored:
                    print(GREEN + "NerdQAxe reset to best or starting settings and results saved." + RESET)
                else:
                    print(RED + RESTORE_FAIL_WARNING + RESET)
            else:
                # No results at all means the run never got a usable measurement
                # (e.g. settings wouldn't apply or the device was unreachable):
                # restore and flag it as a failure for automation.
                print(YELLOW + "No valid benchmarking results found. Restoring the device's starting settings." + RESET)
                if not set_system_settings(default_voltage, default_frequency):
                    print(RED + RESTORE_FAIL_WARNING + RESET)
                exit_code = 1
            system_reset_done = True

        if results:
            save_csv()
            save_final_json()
            print(GREEN + "\nBenchmarking completed." + RESET)
            print_summary()
            if soak_minutes > 0 and not interrupted:
                run_soak(soak_minutes)

    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
