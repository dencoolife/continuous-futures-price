"""
continuous_futures.py — build a continuous daily price series from individual futures contracts,
the right way, and measure what goes wrong when it's done carelessly.

Written for ICE Cotton No. 2 (symbols CT + month code + 2-digit year, e.g. CTZ24 = Dec 2024),
but nothing is cotton-specific except the defaults: change the contract cycle, the first-notice
rule and the daily limits for another market.

Main steps
  1. load_contracts()      one CSV per contract (Barchart "historical data" download format)
  2. first_notice_day()    exchange rule for when delivery notices can start
  3. build_chain()         hold the most-traded contract, roll N trading days before first
                           notice; daily return always measured WITHIN one contract
  4. back-adjusted prices  ratio (multiplicative) and difference ("Panama") adjustment
  5. flag_days()           limit days, thin/stale prices, holiday gaps
  6. adf() / kpss() / acf() stationarity and memory checks (numpy/scipy only)

No data is included with this code. See data/README.md for the expected file format.
"""
import re
from pathlib import Path

import numpy as np
import pandas as pd

MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
               "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
COTTON_CYCLE = ["H", "K", "N", "Z"]            # Mar, May, Jul, Dec (Oct skipped: thin)
COTTON_LIMITS = (3.0, 4.0, 5.0, 6.0, 7.0)      # ICE cotton daily limit range, cents/lb


# ---------------------------------------------------------------- loading
def read_barchart_csv(path):
    """One contract's daily history. Columns: Time, Open, High, Low, Latest (= settlement),
    Change, %Change, Volume, Open Int; a text footer line is skipped."""
    d = pd.read_csv(path)
    d = d[pd.to_datetime(d["Time"], errors="coerce").notna()]
    d = d.rename(columns={"Time": "date", "Open": "open", "High": "high", "Low": "low",
                          "Latest": "settle", "Volume": "volume", "Open Int": "oi"})
    d["date"] = pd.to_datetime(d["date"])
    for c in ["open", "high", "low", "settle", "volume", "oi"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    return d[["date", "open", "high", "low", "settle", "volume", "oi"]].sort_values("date")


def load_contracts(data_dir, pattern="CT*.csv"):
    """Returns (calendar, contracts): the union of all trading days, and a dict
    symbol -> DataFrame indexed on that calendar (NaN where the contract didn't trade)."""
    sym = re.compile(r"^[A-Z]{1,3}[FGHJKMNQUVXZ]\d{2}$")       # e.g. CTZ24; skips other files
    files = sorted(f for f in Path(data_dir).glob(pattern) if sym.match(f.stem.upper()))
    if not files:
        raise FileNotFoundError(
            f"no contract files matching {pattern} in '{data_dir}'. Price data isn't included in "
            "this repo: put one CSV per contract in data/ (see data/README.md), or set the "
            "environment variable FUTURES_DATA_DIR to the folder that holds them and restart Jupyter.")
    raw = {f.stem.upper(): read_barchart_csv(f).set_index("date") for f in files}
    cal = pd.DatetimeIndex(sorted(set().union(*[d.index for d in raw.values()])))
    return cal, {s: d.reindex(cal) for s, d in raw.items()}


def contract_month(sym):
    """'CTZ24' -> Timestamp(2024-12-01)."""
    return pd.Timestamp(2000 + int(sym[-2:]), MONTH_CODES[sym[-3]], 1)


def next_contract(sym, cycle=COTTON_CYCLE):
    root, m, yy = sym[:-3], sym[-3], int(sym[-2:])
    i = cycle.index(m)
    nm = cycle[(i + 1) % len(cycle)]
    return f"{root}{nm}{(yy + (i + 1 == len(cycle))) % 100:02d}"


# ---------------------------------------------------------------- exchange calendar rules
def extend_calendar(cal, periods=80):
    """Add plain business days after the data ends, so rules for contracts that deliver after
    the last data day can still be evaluated (holidays ignored there)."""
    return cal.append(pd.bdate_range(cal[-1] + pd.Timedelta(days=1), periods=periods))


def first_notice_day(sym, cal, business_days=5):
    """ICE cotton: five business days before the first business day of the delivery month,
    i.e. the 5th-last trading day of the month before. `cal` must contain the trading days."""
    return cal[cal < contract_month(sym)][-business_days]


def roll_date(sym, cal, days_before_fnd):
    """The close at which we switch out of `sym`."""
    return cal[cal.get_loc(first_notice_day(sym, cal)) - days_before_fnd]


def volume_crossover(contracts, cal, cycle=COTTON_CYCLE, window=3):
    """For each pair (expiring, next): trading days before first notice when the next contract's
    3-day average volume first exceeds the expiring one's, and when its open interest does.
    This is how traders actually roll, and it is what the roll rule should be based on."""
    ext = extend_calendar(cal)
    rows = []
    for sym, s in contracts.items():
        nxt = next_contract(sym, cycle)
        if sym[-3] not in cycle or nxt not in contracts:
            continue
        if s.settle.last_valid_index() is None or s.settle.last_valid_index() < contract_month(sym) - pd.Timedelta(days=5):
            continue                                        # still trading / not expired
        f = first_notice_day(sym, ext)
        j = pd.DataFrame({"v0": s.volume, "o0": s.oi, "v1": contracts[nxt].volume,
                          "o1": contracts[nxt].oi}).dropna()
        j = j[(j.index > f - pd.Timedelta(days=70)) & (j.index <= f)]
        v = j.v1.rolling(window).mean() > j.v0.rolling(window).mean()
        o = j.o1 > j.o0
        days_before = lambda t: ext.get_loc(f) - ext.get_loc(t) if pd.notna(t) else np.nan
        rows.append(dict(contract=sym, next=nxt, first_notice=f.date(),
                         volume_cross=days_before(v[v].index.min()),
                         oi_cross=days_before(o[o].index.min())))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- the chain
def build_chain(contracts, cal, first, first_prev, days_before_fnd=10, cycle=COTTON_CYCLE):
    """Hold `first` from the day after `first_prev`'s roll, then roll along `cycle` at the close
    `days_before_fnd` trading days before each first notice day.

    Columns returned (one row per trading day)
      contract, settle, prev_settle        the contract held and its settlements
      ret          ln(settle / previous settle) of the SAME contract  <- the clean return
      naive_ret    ln(settle / previous row's settle)  <- what a careless splice gives
      roll_day     True on the close where we switch; next_contract / next_settle / roll_gap
                   (ln(new / old) that day) are filled on those rows
      days_to_fnd  trading days left before the held contract's first notice day
    """
    ext = extend_calendar(cal)
    parts, c, prev = [], first, roll_date(first_prev, ext, days_before_fnd)
    while c in contracts:
        r = roll_date(c, ext, days_before_fnd)
        seg = cal[(cal > prev) & (cal <= r)]
        s = contracts[c]
        d = pd.DataFrame({"contract": c, "open": s.open, "high": s.high, "low": s.low,
                          "settle": s.settle, "prev_settle": s.settle.shift(1),
                          "volume": s.volume, "oi": s.oi}).loc[seg]
        f = first_notice_day(c, ext)
        d["days_to_fnd"] = [ext.get_loc(f) - ext.get_loc(t) for t in seg]
        d["roll_day"] = False
        if r > cal[-1]:                                  # roll not due yet: series ends here
            parts.append(d)
            break
        nc = next_contract(c, cycle)
        d.loc[r, "roll_day"] = True
        d.loc[r, "next_contract"] = nc
        d.loc[r, "next_settle"] = contracts[nc].settle.get(r) if nc in contracts else np.nan
        parts.append(d)
        if nc not in contracts:
            break
        prev, c = r, nc
    a = pd.concat(parts)
    a.index.name = "date"
    a["roll_day"] = a.roll_day.astype(bool)
    a["ret"] = np.log(a.settle / a.prev_settle)
    a["naive_ret"] = np.log(a.settle / a.settle.shift(1))
    a["roll_gap"] = np.log(a.next_settle / a.settle)
    a["roll_type"] = np.where(a.roll_day, a.contract.str[-3] + "->" + a.next_contract.fillna("").str[-3], None)
    return a


def ratio_adjusted(a):
    """Multiplicative back-adjustment: chain the clean returns and scale so the LAST value equals
    today's real settlement. Percentage moves are exact; older levels are not real prices."""
    cum = a.ret.fillna(0).cumsum()
    return a.settle.iloc[-1] * np.exp(cum - cum.iloc[-1])


def difference_adjusted(a):
    """'Panama' back-adjustment: shift every earlier price by the cents gap at each later roll.
    Cent moves are exact; percentage moves (and levels) get distorted, and prices can turn
    negative in long histories."""
    # gap (new - old, cents) at each roll; a day gets every gap from rolls on or after it.
    # The roll day itself still holds the old contract, so it includes that day's gap.
    gap_cents = (a.next_settle - a.settle).where(a.roll_day, 0.0).fillna(0.0)
    return a.settle + gap_cents[::-1].cumsum()[::-1]


# ---------------------------------------------------------------- flags
def flag_days(a, limits=COTTON_LIMITS, min_volume=1000):
    """limit       closed exactly a daily-limit move away from yesterday and at the day's high/low
       thin        fewer than `min_volume` contracts traded
       stale       no volume, or high = low = settle (no real trading)
       holiday_gap more calendar days than a normal weekday/weekend gap
       after_limit the day after a limit close"""
    f = pd.DataFrame(index=a.index)
    chg = a.settle - a.prev_settle
    f["limit"] = chg.abs().round(2).isin(limits) & ((a.settle == a.high) | (a.settle == a.low))
    f["thin"] = a.volume < min_volume
    f["stale"] = (a.volume == 0) | ((a.high == a.low) & (a.low == a.settle))
    gap = a.index.to_series().diff().dt.days
    f["gap_days"] = gap
    f["holiday_gap"] = (gap > 1) & ~((gap == 3) & (a.index.dayofweek == 0))
    f["after_limit"] = f.limit.shift(1, fill_value=False)
    return f


# ---------------------------------------------------------------- statistics (numpy / scipy)
ADF_CV = {"c": {"1%": (-3.43035, -6.5393, -16.786), "5%": (-2.86154, -2.8903, -4.234)},
          "ct": {"1%": (-3.95877, -9.0531, -28.428), "5%": (-3.41049, -4.3904, -9.036)}}
KPSS_CV = {"c": {"5%": 0.463, "1%": 0.739}, "ct": {"5%": 0.146, "1%": 0.216}}


def _ols(y, X):
    b, *_ = np.linalg.lstsq(X, y, rcond=None)
    e = y - X @ b
    s2 = e @ e / (len(y) - X.shape[1])
    return b, np.sqrt(np.diag(s2 * np.linalg.inv(X.T @ X))), e


def adf(y, trend="c"):
    """Augmented Dickey-Fuller t-stat (lag by AIC, then re-fit on the full sample, as statsmodels)
    and MacKinnon (2010) critical values. Stat below the critical value = stationary."""
    y = np.asarray(y, float)
    dy = np.diff(y)
    n = len(dy)
    maxlag = int(12 * (n / 100) ** .25)

    def design(rows, p):
        cols = [np.ones(len(rows)), y[rows]]
        if trend == "ct":
            cols.append(rows.astype(float))
        return np.column_stack(cols + [dy[rows - i] for i in range(1, p + 1)])

    rows = np.arange(maxlag, n)
    aics = []
    for p in range(maxlag + 1):
        _, _, e = _ols(dy[rows], design(rows, p))
        aics.append(len(rows) * np.log(e @ e / len(rows)) + 2 * (p + 2 + (trend == "ct")))
    p = int(np.argmin(aics))
    rows = np.arange(p, n)
    b, se, _ = _ols(dy[rows], design(rows, p))
    T = len(rows)
    cv = {k: v[0] + v[1] / T + v[2] / T ** 2 for k, v in ADF_CV[trend].items()}
    return b[1] / se[1], cv


def kpss(y, trend="c"):
    """KPSS stat (Bartlett long-run variance). Stat above the critical value = NOT stationary."""
    y = np.asarray(y, float)
    n = len(y)
    X = np.ones((n, 1)) if trend == "c" else np.column_stack([np.ones(n), np.arange(n)])
    _, _, e = _ols(y, X)
    S = np.cumsum(e)
    lags = int(12 * (n / 100) ** .25)
    lr = e @ e / n + sum(2 * (1 - k / (lags + 1)) * (e[k:] @ e[:-k]) / n for k in range(1, lags + 1))
    return (S @ S) / (n ** 2 * lr), KPSS_CV[trend]


def acf(x, nlags):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)] - np.nanmean(x)
    d = x @ x
    return np.array([(x[k:] @ x[:-k]) / d for k in range(1, nlags + 1)])


def describe_returns(r):
    """Daily % moves: spread, fat tails, day-to-day memory, biggest day."""
    r = pd.Series(r).dropna() * 100
    return pd.Series({"days": len(r), "std %": r.std(), "excess kurtosis": r.kurt(),
                      "lag-1 autocorr": r.autocorr(1), "biggest day %": r.abs().max(),
                      "days beyond ±3%": int((r.abs() > 3).sum())})
