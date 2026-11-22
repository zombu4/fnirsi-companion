"""Solver of the LC1020E gain calibration (OWN_CAL_DESIGN.md "OCD" 3.2 to 3.4) and the proposals for
the CLIP and SETTLE constants (OCD 6.3).

Model (OCD 2.1): the meter's nominal quotient is Z_nom = Z_x / (zeta[c8][c6] * nu[c5]) for a part of
impedance Z_x read in the cell (c5, c6, c8): zeta (32 per frequency, index 8 * c8 + c6) corrects the
current path, nu (8, index c5) the voltage path. Taking logarithms the model is linear:

    log Z_nom = log Z_x - log zeta[c8][c6] - log nu[c5]

and the real part (log magnitude) and the imaginary part (phase) are two independent weighted least
squares systems per frequency. Unknowns: log zeta (32), log nu (8, one of them fixed to 0: the gauge, a c5
reached at that frequency with the most windows), log Z_x of every resistor. The reference values of the
resistors enter as extra rows ("anchors") weighted by their uncertainty:

  magnitude anchors  100 ohm .. 100 kohm below 5 kHz, 100 ohm .. 10 kohm at 10 kHz and 100 kHz
                     (10 ohm is never a magnitude anchor)
  phase anchors      up to 10 kohm below 5 kHz, 100 ohm .. 2 kohm at 10 kHz and 100 kHz

and the anchor value is Z_s + 1/(1/R + Y_o) with the terminal residuals Z_s (short) and Y_o (open) the
meter read. They are first taken as read in the nominal domain, then referred back through the fitted
factor of the cell they were read in (Z_s * zeta * nu, Y_o / (zeta * nu)) and the fit is repeated once
(OCD 3.2, 3.3 step 3).

Identifiability (OCD 3.3 step 4): a factor's bit is set when its unit row lies in the row space of its
system, tested with the null space of the weighted design matrix (SVD) with the gauge fixed.

Checks (OCD 3.4): residual RMS of the normalised residuals <= 3 per frequency and per (frequency, level);
an anchor with a residual above 3 times its uncertainty is dropped (and the fit repeated); the bounds of
OCD 2.4; every cell that autoranging chose in the run must have all four bits (all-or-nothing per
frequency, OCD 2.1 F12). The result is a GAIN section body and a report.
"""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass, field

import numpy as np

from ..devices import lc1020e as lc

RMS_LIMIT = 3.0                  # OCD 3.4: residual RMS <= 3 x the RMS expected from the weights
ANCHOR_LIMIT = 3.0               # OCD 3.4: anchor residual <= 3 x its uncertainty
LOW_FREQUENCY_LIMIT = 5000       # OCD 3.2: "below 5 kHz" / "at 10 kHz and 100 kHz"
MIN_AMPLITUDE = 300.0            # OCD 3.3 step 2: A_V and A_I at least 300 counts
STEP_MAG_SMALL, STEP_PHASE_SMALL = 1e-3, 0.05    # OCD 3.4 D14: 0.1 percent / 0.05 degrees
VAR_FLOOR = 1e-14                # window variance floor (log units squared)
RANK_TOLERANCE = 1e-9            # singular values below this fraction of the largest are zero
IDENT_TOLERANCE = 1e-7           # |null space component| below this: the unknown is identified
N_ZETA, N_NU = 32, 8


@dataclass
class Resistor:
    """A reference resistor: value in ohm from the owner's DMM, its relative uncertainty (fraction)
    and the uncertainty of its phase (degrees), which is that of the compensated terminal model."""
    name: str
    value: float
    rel_unc: float
    phase_unc_deg: float = 0.05


@dataclass
class Window:
    """One closed window of a resistor in a cell (an OCAL_RAW record)."""
    resistor: int
    fi: int
    level: float
    c5: int
    c6: int
    c8: int
    znom: complex
    var: float
    autoranged: bool = False

    @property
    def cell(self) -> tuple:
        return (self.c5, self.c6, self.c8)


@dataclass
class ShortOpenWindow:
    """One closed window of the short bar or of the open terminals (OCD 3.3 step 2a); `take`
    numbers the insertions of the short bar."""
    kind: str
    fi: int
    level: float
    c5: int
    c6: int
    c8: int
    znom: complex
    var: float
    take: int = 1

    @property
    def cell(self) -> tuple:
        return (self.c5, self.c6, self.c8)


def window_from_raw(raw, resistor: int, fi: int, autoranged: bool = False) -> Window:
    """A Window from an OCAL_RAW record (devices.lc1020e.OcalRaw)."""
    return Window(resistor, fi, round(raw.level, 4), raw.c5, raw.c6, raw.c8, raw.znom,
                  max(raw.variance(), VAR_FLOOR), autoranged)


def shortopen_from_raw(raw, kind: str, fi: int, take: int = 1) -> ShortOpenWindow:
    return ShortOpenWindow(kind, fi, round(raw.level, 4), raw.c5, raw.c6, raw.c8, raw.znom,
                           max(raw.variance(), VAR_FLOOR), take)


def is_usable(raw) -> bool:
    """OCD 3.3 step 2: a window is kept when the cell shows no saturated acquisition and both
    amplitudes are at least 300 counts."""
    return (not raw.saturated and not raw.overload and raw.a_v >= MIN_AMPLITUDE
            and raw.a_i >= MIN_AMPLITUDE and math.isfinite(raw.z_re) and math.isfinite(raw.z_im)
            and raw.znom != 0)


def anchor_kinds(value: float, frequency: float) -> tuple:
    """(magnitude anchor, phase anchor) of a resistor of `value` ohm at `frequency` Hz (OCD 3.2)."""
    if frequency < LOW_FREQUENCY_LIMIT:
        return 100.0 <= value <= 100e3, value <= 10e3
    return 100.0 <= value <= 10e3, 100.0 <= value <= 2e3


def anchor_value(value: float, zs: complex, yo: complex) -> complex:
    """The compensated terminal impedance Z_s + 1 / (1 / R + Y_o) (OCD 3.2)."""
    return zs + 1.0 / (1.0 / value + yo)


@dataclass
class SystemFit:
    """The fit of one system (magnitude or phase) of one frequency."""
    name: str
    x: np.ndarray                # solution over the 40 + R columns (gauge column zero)
    ident: np.ndarray            # bool per column
    rank: int
    columns: int
    rms: float                   # RMS of the normalised window residuals
    level_rms: dict              # level -> RMS
    anchor_res: dict             # resistor index -> normalised anchor residual


@dataclass
class FreqResult:
    """The outcome of one frequency index (OCD 3.4 report)."""
    fi: int
    frequency: int
    windows: int = 0
    ok: bool = False
    reasons: list = field(default_factory=list)
    gauge: int | None = None
    fitted: lc.GainFreq = field(default_factory=lc.GainFreq)    # what the data identified
    entry: lc.GainFreq = field(default_factory=lc.GainFreq)     # what is written (masks 0 when blocked)
    mag_rms: float = math.nan
    phase_rms: float = math.nan
    level_rms: dict = field(default_factory=dict)               # level -> (magnitude, phase)
    anchors: list = field(default_factory=list)                 # (name, kind, residual in sigma, used)
    dropped: list = field(default_factory=list)
    blocked_cells: list = field(default_factory=list)           # autoranged cells without four bits
    zs: complex = 0j
    yo: complex = 0j
    shortopen_notes: list = field(default_factory=list)
    step_mag: float = math.nan
    step_phase: float = math.nan
    small_steps: bool = False
    ident_counts: dict = field(default_factory=dict)            # component -> (identified, reached)


@dataclass
class SolveResult:
    freqs: dict
    resistors: list

    def entries(self) -> list:
        """Six GainFreq entries in the frequency-index order, blocked / absent ones with masks 0."""
        return [self.freqs[fi].entry if fi in self.freqs else lc.GainFreq()
                for fi in range(len(lc.OCAL_FREQ))]

    def section(self) -> bytes:
        """The GAIN section body for the record (OCD 1.2)."""
        return lc.pack_gain(self.entries())

    def written_frequencies(self) -> list:
        return [fr.frequency for fr in self.freqs.values() if fr.ok]

    def report(self) -> str:
        """The text of OCD 3.4: residuals per frequency and level, anchors, steps, identifiability
        per component, the cells and checks that blocked a frequency."""
        lines = []
        for fi in sorted(self.freqs):
            fr = self.freqs[fi]
            lines.append("%d Hz: %s (%d windows)" % (fr.frequency, "GAIN written" if fr.ok
                                                     else "BLOCKED, masks 0", fr.windows))
            for why in fr.reasons:
                lines.append("    blocked: %s" % why)
            if fr.gauge is not None:
                lines.append("    gauge nu[%d] = 1; residual RMS %s magnitude, %s phase (limit %g)"
                             % (fr.gauge, _g(fr.mag_rms), _g(fr.phase_rms), RMS_LIMIT))
            for level, (m, p) in sorted(fr.level_rms.items()):
                lines.append("    level %g V: RMS %s magnitude, %s phase" % (level, _g(m), _g(p)))
            for comp, (n_ok, n_all) in fr.ident_counts.items():
                lines.append("    identified %s: %d of %d reached" % (comp, n_ok, n_all))
            for name, kind, res, used in fr.anchors:
                lines.append("    anchor %s %s: residual %s sigma%s" % (
                    name, kind, _g(res), "" if used else " (dropped)"))
            if fr.zs or fr.yo:
                lines.append("    terminals: Zs %s ohm, Yo %s S (referred back through the fitted "
                             "factors)" % (_c(fr.zs), _c(fr.yo)))
            for note in fr.shortopen_notes:
                lines.append("    short/open: %s" % note)
            if fr.blocked_cells:
                lines.append("    autoranged cells without all four bits (c5, c6, c8): %s"
                             % ", ".join(str(c) for c in fr.blocked_cells))
            if not math.isnan(fr.step_mag):
                lines.append("    largest step between neighbouring codes: %.3g %% / %.3g degrees%s"
                             % (fr.step_mag * 100, fr.step_phase, "; the correction changes little "
                                "(below 0.1 % / 0.05 degrees): you decide whether to write it"
                                if fr.small_steps else ""))
        if not lines:
            lines.append("no data")
        return "\n".join(lines)


def _g(v: float) -> str:
    return "n/a" if math.isnan(v) else "%.3g" % v


def _c(z: complex) -> str:
    return "%.4g%+.4gj" % (z.real, z.imag)


# ----------------------------------------------------------------------------------------------
# One system of one frequency
# ----------------------------------------------------------------------------------------------

def _fit_system(name: str, wins: list, resistors: list, anchors: dict, gauge: int) -> SystemFit:
    """Weighted least squares of the magnitude (name "magnitude") or phase system. `anchors` maps a
    resistor index to (value of log or phase, variance)."""
    n_res = len(resistors)
    ncol = N_ZETA + N_NU + n_res
    rows = len(wins) + len(anchors)
    a = np.zeros((rows, ncol))
    y = np.zeros(rows)
    var = np.zeros(rows)
    for i, w in enumerate(wins):
        a[i, 8 * w.c8 + w.c6] = -1.0
        if w.c5 != gauge:
            a[i, N_ZETA + w.c5] = -1.0
        a[i, N_ZETA + N_NU + w.resistor] = 1.0
        y[i] = math.log(abs(w.znom)) if name == "magnitude" else math.atan2(w.znom.imag, w.znom.real)
        var[i] = w.var
    anchor_rows = {}
    for j, (r, (val, v)) in enumerate(sorted(anchors.items())):
        i = len(wins) + j
        a[i, N_ZETA + N_NU + r] = 1.0
        y[i], var[i] = val, v
        anchor_rows[r] = i
    sw = 1.0 / np.sqrt(var)
    aw, yw = a * sw[:, None], y * sw
    x = np.linalg.lstsq(aw, yw, rcond=None)[0]
    # the null space needs a square V: pad with zero rows when there are fewer rows than columns
    padded = aw if rows >= ncol else np.vstack([aw, np.zeros((ncol - rows, ncol))])
    _u, s, vt = np.linalg.svd(padded, full_matrices=False)
    rank = int((s > s[0] * RANK_TOLERANCE).sum()) if s.size and s[0] > 0 else 0
    null = vt[rank:].T                              # ncol x (ncol - rank)
    ident = np.linalg.norm(null, axis=1) < IDENT_TOLERANCE if null.size else np.ones(ncol, bool)
    ident[N_ZETA + gauge] = True                    # fixed (its column is all zero), so known
    res = yw - aw @ x
    nw = len(wins)
    rms = float(np.sqrt(np.mean(res[:nw] ** 2))) if nw else math.nan
    level_rms = {}
    for level in sorted({w.level for w in wins}):
        idx = [i for i, w in enumerate(wins) if w.level == level]
        level_rms[level] = float(np.sqrt(np.mean(res[idx] ** 2)))
    anchor_res = {r: float(res[i]) for r, i in anchor_rows.items()}
    return SystemFit(name, x, ident, rank, ncol, rms, level_rms, anchor_res)


def _anchor_set(system: str, resistors: list, freq: int, zs: complex, yo: complex, dropped: set,
                with_windows: set, repeat: float) -> dict:
    """{resistor index: (anchor value, variance)} of one system at one frequency."""
    out = {}
    for r, res in enumerate(resistors):
        if r in dropped or r not in with_windows:
            continue
        mag_ok, phase_ok = anchor_kinds(res.value, freq)
        za = anchor_value(res.value, zs, yo)
        extra = (repeat / res.value) ** 2 if res.value <= 101.0 else 0.0
        if system == "magnitude" and mag_ok:
            out[r] = (math.log(abs(za)), max(res.rel_unc, 1e-6) ** 2 + extra)
        elif system == "phase" and phase_ok:
            out[r] = (math.atan2(za.imag, za.real),
                      max(math.radians(res.phase_unc_deg), 1e-6) ** 2 + extra)
    return out


def _masks(mag: SystemFit, ph: SystemFit, reached_z: set, reached_n: set) -> lc.GainFreq:
    """The identified bits and factors of one frequency. A factor whose magnitude is not identified
    keeps magnitude 1, one whose phase is not identified keeps phase 0, so that every factor with a
    bit set is a sane value for the meter's checks (OCD 2.4) whichever part the data fixed."""
    g = lc.GainFreq()

    def factor(k, col, reached, mag_mask_name, ph_mask_name, bit):
        m_ok = col in reached and mag.ident[k]
        p_ok = col in reached and ph.ident[k]
        if m_ok:
            setattr(g, mag_mask_name, getattr(g, mag_mask_name) | 1 << bit)
        if p_ok:
            setattr(g, ph_mask_name, getattr(g, ph_mask_name) | 1 << bit)
        return (math.exp(mag.x[k]) if m_ok else 1.0) * cmath.exp(1j * float(ph.x[k]) if p_ok else 0j)

    for k in range(N_ZETA):
        g.zeta[k] = factor(k, k, reached_z, "zeta_mag_mask", "zeta_phase_mask", k)
    for c in range(N_NU):
        g.nu[c] = factor(N_ZETA + c, c, reached_n, "nu_mag_mask", "nu_phase_mask", c)
    return g


def _cell_factor(g: lc.GainFreq, c5: int, c6: int, c8: int) -> complex | None:
    """zeta * nu of a cell when all four bits are set, else None."""
    if not g.cell_calibrated(c5, c6, c8):
        return None
    return g.zeta[8 * c8 + c6] * g.nu[c5]


def _referral(shortopen: list, g: lc.GainFreq) -> tuple:
    """(Zs, Yo, notes) referred back through the factors of the cells they were read in; a cell
    without all four bits is not corrected and named in the notes."""
    notes = []
    zs_acc, zs_den = 0j, 0.0
    yo_acc, yo_den = 0j, 0.0
    for w in shortopen:
        f = _cell_factor(g, w.c5, w.c6, w.c8)
        if f is None:
            f = 1 + 0j
            note = "%s cell (c5=%d, c6=%d, c8=%d) is not calibrated: read in the nominal domain" % (
                w.kind, w.c5, w.c6, w.c8)
            if note not in notes and g.any_cell():
                notes.append(note)
        wt = 1.0 / w.var
        if w.kind == "short":
            zs_acc += wt * w.znom * f
            zs_den += wt
        else:
            yo_acc += wt * (1.0 / w.znom) / f
            yo_den += wt
    return (zs_acc / zs_den if zs_den else 0j), (yo_acc / yo_den if yo_den else 0j), notes


def _short_repeat(shortopen: list, g: lc.GainFreq | None = None) -> float:
    """|Zs(take 1) - Zs(take 2)| in ohm, 0 with fewer than two takes (OCD 3.3 step 2a)."""
    takes: dict = {}
    for w in shortopen:
        if w.kind == "short":
            f = _cell_factor(g, w.c5, w.c6, w.c8) if g is not None else None
            takes.setdefault(w.take, []).append((w.znom * (f if f is not None else 1.0), 1.0 / w.var))
    means = [sum(z * wt for z, wt in v) / sum(wt for _z, wt in v) for _t, v in sorted(takes.items())]
    return abs(means[0] - means[1]) if len(means) >= 2 else 0.0


# ----------------------------------------------------------------------------------------------
# One frequency, then the run
# ----------------------------------------------------------------------------------------------

def solve_frequency(fi: int, wins: list, resistors: list, shortopen: list | None = None) -> FreqResult:
    """Steps 3 to 5 of OCD 3.3 and the checks of OCD 3.4 for one frequency index from its windows
    (all resistors, all levels) and the short / open windows of that frequency."""
    freq = lc.OCAL_FREQ[fi]
    fr = FreqResult(fi, freq, windows=len(wins))
    shortopen = [w for w in (shortopen or []) if w.fi == fi]
    if not wins:
        fr.reasons.append("no windows")
        return fr
    counts: dict = {}
    for w in wins:
        counts[w.c5] = counts.get(w.c5, 0) + 1
    gauge = max(sorted(counts), key=lambda c: counts[c])
    fr.gauge = gauge
    with_windows = {w.resistor for w in wins}
    reached_z = {8 * w.c8 + w.c6 for w in wins}
    reached_n = {w.c5 for w in wins}

    # first pass: the terminal residuals as the meter read them (nominal domain)
    zs, yo, _ = _referral(shortopen, lc.GainFreq())
    dropped: set = set()
    repeat = _short_repeat(shortopen)

    def fit_both(zs_, yo_, rep):
        while True:
            am = _anchor_set("magnitude", resistors, freq, zs_, yo_, dropped, with_windows, rep)
            ap = _anchor_set("phase", resistors, freq, zs_, yo_, dropped, with_windows, rep)
            mag = _fit_system("magnitude", wins, resistors, am, gauge)
            ph = _fit_system("phase", wins, resistors, ap, gauge)
            worst = None
            for sysfit in (mag, ph):
                for r, res in sysfit.anchor_res.items():
                    if abs(res) > ANCHOR_LIMIT and (worst is None or abs(res) > worst[1]):
                        worst = (r, abs(res))
            if worst is None:
                return mag, ph
            dropped.add(worst[0])
            fr.dropped.append("%s (%.1f sigma)" % (resistors[worst[0]].name, worst[1]))

    mag, ph = fit_both(zs, yo, repeat)
    if shortopen:
        first = _masks(mag, ph, reached_z, reached_n)
        zs, yo, notes = _referral(shortopen, first)
        repeat = _short_repeat(shortopen, first)
        mag, ph = fit_both(zs, yo, repeat)
        fr.shortopen_notes = notes
        if repeat:
            fr.shortopen_notes.append("short taken twice: |dZs| = %.4g ohm added to the 100 ohm "
                                      "anchor's uncertainty" % repeat)
    else:
        fr.shortopen_notes.append("no short / open windows: anchors without terminal residuals")
    fr.zs, fr.yo = zs, yo
    fitted = _masks(mag, ph, reached_z, reached_n)
    fr.fitted = fitted
    fr.mag_rms, fr.phase_rms = mag.rms, ph.rms
    for level in sorted(set(mag.level_rms) | set(ph.level_rms)):
        fr.level_rms[level] = (mag.level_rms.get(level, math.nan), ph.level_rms.get(level, math.nan))
    for name, sysfit in (("magnitude", mag), ("phase", ph)):
        for r, res in sorted(sysfit.anchor_res.items()):
            fr.anchors.append((resistors[r].name, name, res, True))
    for r in sorted(dropped):
        fr.anchors.append((resistors[r].name, "magnitude and phase", math.nan, False))
    fr.ident_counts = {
        "zeta magnitude": (bin(fitted.zeta_mag_mask).count("1"), len(reached_z)),
        "zeta phase": (bin(fitted.zeta_phase_mask).count("1"), len(reached_z)),
        "nu magnitude": (bin(fitted.nu_mag_mask).count("1"), len(reached_n)),
        "nu phase": (bin(fitted.nu_phase_mask).count("1"), len(reached_n)),
    }

    # D14: steps between neighbouring codes
    sm = sp = 0.0
    pairs = 0
    for c8 in range(4):
        for c6 in range(7):
            a, b = 8 * c8 + c6, 8 * c8 + c6 + 1
            if _both(fitted, "zeta", a, b):
                sm, sp, pairs = _step(sm, sp, pairs, fitted.zeta[b] / fitted.zeta[a])
    for c5 in range(7):
        if _both(fitted, "nu", c5, c5 + 1):
            sm, sp, pairs = _step(sm, sp, pairs, fitted.nu[c5 + 1] / fitted.nu[c5])
    if pairs:
        fr.step_mag, fr.step_phase = sm, sp
        fr.small_steps = sm < STEP_MAG_SMALL and sp < STEP_PHASE_SMALL

    # checks (OCD 3.4) and the all-or-nothing rule (OCD 2.1 F12, 3.3 step 5)
    for what, sysfit in (("magnitude", mag), ("phase", ph)):
        if not sysfit.rms <= RMS_LIMIT:
            fr.reasons.append("%s residual RMS %.3g is above %g: the separable model does not "
                              "hold at this frequency" % (what, sysfit.rms, RMS_LIMIT))
        for level, v in sysfit.level_rms.items():
            if not v <= RMS_LIMIT:
                fr.reasons.append("%s residual RMS %.3g at level %g V is above %g: the factors "
                                  "depend on the excitation level" % (what, v, level, RMS_LIMIT))
    bad = lc.check_gain_entry(fitted, fi)
    if bad:
        fr.reasons.append("outside the bounds of OCD 2.4 (%s)" % bad[1])
    auto = sorted({w.cell for w in wins if w.autoranged})
    if not auto:
        fr.reasons.append("no autoranged cell was recorded: the cells that autoranging reaches "
                          "are not known")
    for c5, c6, c8 in auto:
        if not fitted.cell_calibrated(c5, c6, c8):
            fr.blocked_cells.append((c5, c6, c8))
    if fr.blocked_cells:
        fr.reasons.append("%d autoranged cell(s) lack magnitude or phase bits" % len(fr.blocked_cells))
    fr.ok = not fr.reasons
    fr.entry = fitted if fr.ok else lc.GainFreq()
    return fr


def _both(g: lc.GainFreq, which: str, a: int, b: int) -> bool:
    masks = ((g.zeta_mag_mask, g.zeta_phase_mask) if which == "zeta"
             else (g.nu_mag_mask, g.nu_phase_mask))
    return all((m >> k) & 1 for k in (a, b) for m in masks)


def _step(sm: float, sp: float, pairs: int, ratio: complex) -> tuple:
    return (max(sm, abs(abs(ratio) - 1.0)),
            max(sp, abs(math.degrees(math.atan2(ratio.imag, ratio.real)))), pairs + 1)


def solve(windows: list, resistors: list, shortopen: list | None = None) -> SolveResult:
    """The solver for a whole run: windows of all frequencies, resistors, the short / open windows.
    Frequencies without windows are left out of the result (their entries are written with masks 0)."""
    freqs = {}
    for fi in range(len(lc.OCAL_FREQ)):
        wins = [w for w in windows if w.fi == fi]
        if wins:
            freqs[fi] = solve_frequency(fi, wins, resistors, shortopen)
    return SolveResult(freqs, list(resistors))


# ----------------------------------------------------------------------------------------------
# Proposals for CLIP (OCD 2.4, 6.3) and SETTLE (OCD 2.3, 6.3)
# ----------------------------------------------------------------------------------------------

def propose_clip(observed: dict, margin: int = 16) -> list:
    """Proposed CLIP entries from lock sweeps: observed[(channel, code)] = (lowest minimum,
    highest maximum) in ADC counts over the parts of the sweep (open, short, 1 kohm). When the
    highest maximum reaches 4095 the ADC itself clips and hi stays 4095; otherwise the analog stage
    clips below it and hi = maximum - margin. lo likewise (0 when the minimum reaches 0, else
    minimum + margin). Entries without data keep 0 / 4095. Returns 16 (lo, hi) pairs, index
    8 * channel + code; the meter's bounds (OCD 2.4) are checked when the body is built."""
    out = []
    for ch in range(2):
        for code in range(8):
            mn, mx = observed.get((ch, code), (0, 4095))
            lo = 0 if mn <= 0 else min(mn + margin, 4095)
            hi = 4095 if mx >= 4095 else max(mx - margin, 0)
            out.append((int(lo), int(hi)))
    return out


def propose_settle(entries: list, change_tick: int) -> int | None:
    """Settle time in ms from a trace (OCD 6.3): the time after the change at which |Zm - final|
    stays below 0.1 x the window spread. final = mean of the last 8 entries, the spread = the
    largest |Zm - final| of the recorded acquisitions (the size of the transient); the time is
    that of the first entry from which every later entry is inside the 0.1 band. Rounded up to a
    whole millisecond and limited to 1 .. 500 (OCD 2.4). None with fewer than 8 entries or no
    transient (spread 0)."""
    if len(entries) < 8:
        return None
    z = [e.z for e in entries]
    final = sum(z[-8:]) / 8.0
    dev = [abs(v - final) for v in z]
    spread = max(dev)
    if spread <= 0:
        return None
    band = 0.1 * spread
    first = len(z)
    for i in range(len(z) - 1, -1, -1):
        if dev[i] <= band:
            first = i
        else:
            break
    ms = max(entries[first].tick - change_tick, 0) if first < len(z) else lc.SETTLE_MAX
    return int(min(max(math.ceil(ms), lc.SETTLE_MIN), lc.SETTLE_MAX))
