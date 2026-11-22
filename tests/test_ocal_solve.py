"""Tests of core/ocal_solve.py (OWN_CAL_DESIGN.md "OCD" 3.2 to 3.4, 6.3) on synthetic data: factors are
injected into the nominal quotient Z_nom = Z_x / (zeta[c8][c6] * nu[c5]) of resistors with exactly known
values, with a window variance of 1e-12 (sigma 1e-6 in log units) and no noise, so the solver must give
the products zeta * nu of the injected factors back. The gauge fixes nu of one c5, so only the products
(and the ratios of neighbouring factors) are compared, never the factors themselves.

Cells of the data set: c8 in 0..2, c6 in 1..3, c5 in 1..3 (27 cells). Their zeta indices 8 * c8 + c6 are
{1, 2, 3, 9, 10, 11, 17, 18, 19}: mask 0b1110 << 0 | 0b1110 << 8 | 0b1110 << 16 = 0xE0E0E; nu indices
{1, 2, 3}: mask 0b1110 = 14.
"""

import cmath
import math
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from fnirsi_companion.core import ocal_solve as sv          # noqa: E402
from fnirsi_companion.devices import lc1020e as lc          # noqa: E402
import fake_lc1020e as fk                                    # noqa: E402

CELLS = [(c5, c6, c8) for c8 in range(3) for c6 in (1, 2, 3) for c5 in (1, 2, 3)]
MASK_Z = 0xE0E0E
MASK_N = 14
VAR = 1e-12


def true_zeta(c8, c6):
    return (1 + 0.002 * c6 - 0.003 * c8) * cmath.exp(1j * (0.0004 * c6 + 0.0003 * c8))


def true_nu(c5):
    return (1 + 0.004 * c5) * cmath.exp(1j * 0.0005 * c5)


def small_zeta(c8, c6):
    return (1 + 0.0002 * c6 + 0.0001 * c8) * cmath.exp(1j * 0.0001 * c6)


def small_nu(c5):
    return 1 + 0.0003 * c5


def windows_for(idx, zx, fi=2, level=0.6, cells=CELLS, auto=None, var=VAR, scale=1.0,
                zeta=true_zeta, nu=true_nu):
    """The windows of resistor `idx` of true impedance zx: Z_nom = zx / (zeta * nu) * scale in every
    cell; `auto` is the autoranged cell (c5, c6, c8) or None."""
    return [sv.Window(idx, fi, level, c5, c6, c8, zx / (zeta(c8, c6) * nu(c5)) * scale, var,
                      autoranged=(auto == (c5, c6, c8))) for (c5, c6, c8) in cells]


def decade_run(values=(100.0, 1000.0, 10000.0), zxs=None, **kw):
    """Windows of one resistor per value, each autoranged in (2, 2, c8) with c8 = its index."""
    zxs = zxs or [complex(v) for v in values]
    out = []
    for i, zx in enumerate(zxs):
        out += windows_for(i, zx, auto=(2, 2, i), **kw)
    return out


def resistors_of(values=(100.0, 1000.0, 10000.0), names=None):
    return [sv.Resistor((names or ["R%g" % v for v in values])[i], v, 1e-3)
            for i, v in enumerate(values)]


def check_products(tc, fr, tol=1e-6, cells=CELLS, zeta=true_zeta, nu=true_nu):
    for c5, c6, c8 in cells:
        got = fr.fitted.zeta[8 * c8 + c6] * fr.fitted.nu[c5]
        want = zeta(c8, c6) * nu(c5)
        tc.assertLess(abs(got / want - 1), tol, (c5, c6, c8))


class Recovery(unittest.TestCase):
    def test_injected_factors_are_recovered(self):
        fr = sv.solve_frequency(2, decade_run(), resistors_of())
        self.assertEqual(fr.reasons, [])
        self.assertTrue(fr.ok)
        self.assertEqual(fr.gauge, 1)                       # every c5 has 27 windows: the first of them
        self.assertEqual((fr.fitted.zeta_mag_mask, fr.fitted.zeta_phase_mask), (MASK_Z, MASK_Z))
        self.assertEqual((fr.fitted.nu_mag_mask, fr.fitted.nu_phase_mask), (MASK_N, MASK_N))
        self.assertLess(abs(fr.fitted.nu[1] - 1), 1e-12)    # the gauge
        check_products(self, fr)
        self.assertEqual(fr.entry, fr.fitted)               # written as fitted
        self.assertLess(fr.mag_rms, 1e-3)
        self.assertLess(fr.phase_rms, 1e-3)
        self.assertEqual(sorted(fr.level_rms), [0.6])
        self.assertEqual(fr.blocked_cells, [])
        self.assertEqual(fr.ident_counts["zeta magnitude"], (9, 9))
        self.assertEqual(fr.ident_counts["nu phase"], (3, 3))
        self.assertIsNone(lc.check_gain_entry(fr.entry, 2))

    def test_steps_between_neighbouring_codes(self):
        fr = sv.solve_frequency(2, decade_run(), resistors_of())
        # the largest magnitude step is nu[2] / nu[1] = 1.008 / 1.004 (zeta steps are about 0.2 %), the
        # largest phase step is that of nu, 0.0005 rad
        self.assertAlmostEqual(fr.step_mag, 1.008 / 1.004 - 1, delta=1e-5)
        self.assertAlmostEqual(fr.step_phase, math.degrees(0.0005), delta=1e-4)
        self.assertFalse(fr.small_steps)
        small = decade_run(zeta=small_zeta, nu=small_nu)
        fr = sv.solve_frequency(2, small, resistors_of())
        self.assertTrue(fr.ok)
        check_products(self, fr, 1e-6, zeta=small_zeta, nu=small_nu)
        self.assertLess(fr.step_mag, 1e-3)
        self.assertLess(fr.step_phase, 0.05)
        self.assertTrue(fr.small_steps)
        res = sv.SolveResult({2: fr}, resistors_of())
        self.assertIn("changes little", res.report())

    def test_the_section_and_the_report(self):
        res = sv.solve(decade_run(), resistors_of())
        self.assertEqual(sorted(res.freqs), [2])
        self.assertEqual(res.written_frequencies(), [1000])
        sec = res.section()
        self.assertEqual(len(sec), 1992)
        lc.validate_section(lc.SEC_GAIN, sec)               # the meter's rules (OCD 2.4) hold
        entries = lc.unpack_gain(sec)
        self.assertEqual((entries[2].zeta_mag_mask, entries[2].nu_phase_mask), (MASK_Z, MASK_N))
        self.assertTrue(all(e.zeta_mag_mask == 0 and e.nu_mag_mask == 0
                            for i, e in enumerate(entries) if i != 2))
        rel = entries[2].zeta[10] * entries[2].nu[2] / (true_zeta(1, 2) * true_nu(2)) - 1
        self.assertLess(abs(rel), 1e-5)                     # after the f32 of the section
        text = res.report()
        for part in ("1000 Hz: GAIN written", "gauge nu[1] = 1", "level 0.6 V",
                     "identified zeta magnitude: 9 of 9", "anchor R100 magnitude",
                     "largest step between neighbouring codes"):
            self.assertIn(part, text)
        none = sv.solve([], resistors_of())
        self.assertEqual((none.freqs, none.report(), none.written_frequencies()), ({}, "no data", []))
        self.assertTrue(all(e.zeta_mag_mask == 0 for e in none.entries()))
        self.assertEqual(len(none.section()), 1992)


class AllOrNothing(unittest.TestCase):
    def test_no_phase_anchor_in_reach_sets_magnitude_bits_only(self):
        # 20 kohm and 50 kohm are magnitude anchors below 5 kHz (100 ohm .. 100 kohm) but no phase
        # anchors (up to 10 kohm): the phase system has the null direction (x + t, zeta + t)
        values = (20e3, 50e3)
        wins = []
        for i, v in enumerate(values):
            wins += windows_for(i, complex(v), auto=(2, 2, i))
        fr = sv.solve_frequency(2, wins, resistors_of(values))
        self.assertEqual(fr.fitted.zeta_mag_mask, MASK_Z)
        self.assertEqual(fr.fitted.zeta_phase_mask, 0)
        self.assertEqual((fr.fitted.nu_mag_mask, fr.fitted.nu_phase_mask), (MASK_N, MASK_N))   # nu is identified
        self.assertIsNone(lc.check_gain_entry(fr.fitted, 2))           # magnitude 1 / phase 0 where unknown
        self.assertFalse(fr.ok)
        self.assertEqual(fr.blocked_cells, [(2, 2, 0), (2, 2, 1)])
        self.assertTrue(any("lack magnitude or phase bits" in r for r in fr.reasons))
        self.assertEqual((fr.entry.zeta_mag_mask, fr.entry.nu_mag_mask), (0, 0))      # written with masks 0
        self.assertFalse(fr.entry.any_cell())
        self.assertIn("BLOCKED", sv.SolveResult({2: fr}, []).report())

    def test_one_unidentified_autoranged_cell_blocks_the_frequency(self):
        # a 10 ohm part read only in the cell c8 = 3, c6 = 6 (zeta index 30): 10 ohm is no magnitude
        # anchor, so zeta[30] and the part's own value cannot be told apart in magnitude; below 5 kHz
        # it is a phase anchor, so the phase is identified: magnitude bit 30 clear, phase bit 30 set
        wins = decade_run()
        wins += windows_for(3, 10 + 0j, cells=[(c5, 6, 3) for c5 in (1, 2, 3)], auto=(2, 6, 3))
        fr = sv.solve_frequency(2, wins, resistors_of((100.0, 1000.0, 10000.0, 10.0)))
        self.assertEqual(fr.fitted.zeta_mag_mask, MASK_Z)
        self.assertEqual(fr.fitted.zeta_phase_mask, MASK_Z | 1 << 30)
        self.assertEqual(fr.blocked_cells, [(2, 6, 3)])
        self.assertFalse(fr.ok)
        self.assertEqual((fr.entry.zeta_mag_mask, fr.entry.zeta_phase_mask), (0, 0))
        check_products(self, fr)                            # the cells of the three decades stay exact

    def test_no_autoranged_cell_recorded_blocks(self):
        wins = []
        for i, zx in enumerate((100.0, 1000.0, 10000.0)):
            wins += windows_for(i, complex(zx))               # none flagged autoranged
        fr = sv.solve_frequency(2, wins, resistors_of())
        self.assertFalse(fr.ok)
        self.assertTrue(any("no autoranged cell" in r for r in fr.reasons))
        self.assertEqual(fr.fitted.zeta_mag_mask, MASK_Z)

    def test_a_level_dependent_factor_blocks_and_names_the_level(self):
        wins = []
        for i, zx in enumerate((100.0, 1000.0, 10000.0)):
            wins += windows_for(i, complex(zx), level=0.1, auto=(2, 2, i))
            wins += windows_for(i, complex(zx), level=0.6, auto=(2, 2, i), scale=1.01)   # 1 % at 0.6 V
        fr = sv.solve_frequency(2, wins, resistors_of())
        self.assertFalse(fr.ok)
        self.assertEqual(sorted(fr.level_rms), [0.1, 0.6])
        self.assertGreater(fr.level_rms[0.1][0], 3)
        self.assertGreater(fr.level_rms[0.6][0], 3)
        self.assertLess(fr.level_rms[0.1][1], 3)              # the phase does not depend on it
        for level in ("0.1", "0.6"):
            self.assertTrue(any("at level %s V" % level in r and "excitation level" in r
                                for r in fr.reasons), level)
        self.assertEqual(fr.entry.zeta_mag_mask, 0)

    def test_bounds_of_the_meter_block_a_frequency(self):
        wins = decade_run(zeta=lambda c8, c6: 3.0 + 0j)       # zeta 3: outside 0.5 .. 2 in magnitude
        fr = sv.solve_frequency(2, wins, resistors_of())
        self.assertFalse(fr.ok)
        self.assertTrue(any("bounds of OCD 2.4" in r for r in fr.reasons))


class TerminalResiduals(unittest.TestCase):
    ZS = 2 + 1j                      # ohm: a large short residual, so that the referral shows
    YO = 1e-7j                       # siemens

    def data(self, extra_take=None):
        values = (100.0, 1000.0, 10000.0)
        zxs = [self.ZS + 1.0 / (1.0 / v + self.YO) for v in values]
        wins = decade_run(zxs=zxs)
        cell = (2, 2, 1)
        f = true_zeta(cell[2], cell[1]) * true_nu(cell[0])
        so = [sv.ShortOpenWindow("short", 2, 0.6, *cell, self.ZS / f, VAR) for _ in range(8)]
        so += [sv.ShortOpenWindow("open", 2, 0.6, *cell, 1.0 / self.YO / f, VAR) for _ in range(8)]
        if extra_take is not None:
            so += [sv.ShortOpenWindow("short", 2, 0.6, *cell, (self.ZS + extra_take) / f, VAR, take=2)
                   for _ in range(8)]
        return wins, so

    def test_anchors_use_the_referred_back_terminals(self):
        wins, so = self.data()
        fr = sv.solve_frequency(2, wins, resistors_of(), so)
        self.assertTrue(fr.ok, fr.reasons)
        check_products(self, fr, 1e-4)                         # 2 % anchor error without the compensation
        self.assertLess(abs(fr.zs - self.ZS), 5e-3)
        self.assertLess(abs(fr.yo - self.YO), 1e-9)
        self.assertEqual(fr.shortopen_notes, [])
        self.assertEqual(fr.dropped, [])
        self.assertIn("terminals: Zs", sv.SolveResult({2: fr}, []).report())

    def test_a_second_take_of_the_short_enters_the_anchor(self):
        wins, so = self.data(extra_take=0.01)
        fr = sv.solve_frequency(2, wins, resistors_of(), so)
        self.assertTrue(any("short taken twice" in n for n in fr.shortopen_notes), fr.shortopen_notes)
        w1 = sv.ShortOpenWindow("short", 2, 0.6, 2, 2, 1, 1.0 + 0j, VAR, 1)
        w2 = sv.ShortOpenWindow("short", 2, 0.6, 2, 2, 1, 1.1 + 0j, VAR, 2)
        self.assertAlmostEqual(sv._short_repeat([w1, w2]), 0.1, places=12)
        self.assertEqual(sv._short_repeat([w1]), 0.0)

    def test_a_cell_the_fit_does_not_calibrate_is_named(self):
        wins, so = self.data()
        far = [sv.ShortOpenWindow("short", 2, 0.6, 7, 7, 3, self.ZS, VAR) for _ in range(4)]
        fr = sv.solve_frequency(2, wins, resistors_of(), so + far)
        self.assertTrue(any("(c5=7, c6=7, c8=3) is not calibrated" in n for n in fr.shortopen_notes))

    def test_anchor_value(self):
        z = sv.anchor_value(100.0, self.ZS, self.YO)           # 2 + 1j + 1 / (0.01 + 1e-7 j)
        self.assertAlmostEqual(z.real, 102.0, places=6)
        self.assertAlmostEqual(z.imag, 0.999, places=6)
        self.assertLess(abs(sv.anchor_value(1000.0, 0j, 0j) - 1000), 1e-9)


class WrongReference(unittest.TestCase):
    def test_an_anchor_that_disagrees_is_dropped_and_shown(self):
        # the 1 kohm part is typed as 1100 ohm: its anchor is 95 sigma off; the common scale the three
        # anchors give is pulled by a third of that, so its residual (63.6 sigma) is the largest and it is
        # the one dropped; the other two are consistent after that
        res = resistors_of((100.0, 1100.0, 10000.0), ["R100", "R1k typed 1100", "R10k"])
        fr = sv.solve_frequency(2, decade_run(), res)
        self.assertEqual(len(fr.dropped), 1)
        self.assertTrue(fr.dropped[0].startswith("R1k typed 1100 ("), fr.dropped)
        self.assertTrue(fr.ok, fr.reasons)
        check_products(self, fr, 1e-6)
        self.assertIn("(dropped)", sv.SolveResult({2: fr}, res).report())


class SmallPieces(unittest.TestCase):
    def test_anchor_kinds(self):
        table = [(100.0, 1000, (True, True)), (10.0, 1000, (False, True)),
                 (1e5, 1000, (True, False)), (1e6, 1000, (False, False)),
                 (100.0, 10000, (True, True)), (5000.0, 10000, (True, False)),
                 (1e4, 100000, (True, False)), (1e5, 10000, (False, False)),
                 (10.0, 100000, (False, False)), (2000.0, 100000, (True, True)),
                 (1e5, 5000, (False, False)), (60.0, 60, (False, True))]
        for value, freq, want in table:
            self.assertEqual(sv.anchor_kinds(value, freq), want, (value, freq))

    def test_usable_windows_and_weights(self):
        good = lc.parse_ocal_raw(fk.build_ocal_raw())          # A_V 2000, A_I 1500, sigma^2 1 / 1, N 30
        self.assertTrue(sv.is_usable(good))
        for kw in (dict(a_v=100.0), dict(a_i=299.9), dict(flags=1), dict(flags=2), dict(z=0j)):
            self.assertFalse(sv.is_usable(lc.parse_ocal_raw(fk.build_ocal_raw(**kw))), kw)
        w = sv.window_from_raw(good, 3, 2, True)
        self.assertEqual((w.resistor, w.fi, w.level, w.cell, w.znom, w.autoranged),
                         (3, 2, round(good.level, 4), (2, 2, 1), 1000 + 0j, True))
        self.assertAlmostEqual(w.var, (1 / 4e6 + 1 / 2.25e6) / 1920, delta=1e-15)   # / (64 * 30)
        so = sv.shortopen_from_raw(good, "open", 2, 2)
        self.assertEqual((so.kind, so.fi, so.take, so.cell), ("open", 2, 2, (2, 2, 1)))
        floor = lc.parse_ocal_raw(fk.build_ocal_raw(s2v=0.0, s2i=0.0))
        self.assertEqual(sv.window_from_raw(floor, 0, 0).var, sv.VAR_FLOOR)

    def test_propose_clip(self):
        # (min, max) over the parts: the ADC clips at 4095 (hi stays), or the analog stage clips below it
        obs = {(0, 3): (5, 4095), (1, 2): (40, 4010), (0, 7): (0, 4095)}
        prop = sv.propose_clip(obs, 16)
        self.assertEqual(len(prop), 16)
        self.assertEqual(prop[3], (21, 4095))                  # 5 + 16
        self.assertEqual(prop[8 + 2], (56, 3994))              # 40 + 16, 4010 - 16
        self.assertEqual(prop[7], (0, 4095))                   # the minimum reaches 0: lo stays 0
        self.assertEqual(prop[0], (0, 4095))                   # no data: the defaults
        lc.validate_section(lc.SEC_CLIP, lc.pack_clip(prop))

    @staticmethod
    def trace_entries(z):
        return [lc.TraceEntry(1000 + 5 * (i + 1), v.real, v.imag, 0, 2, 2, 1) for i, v in enumerate(z)]

    def test_propose_settle(self):
        # Zm_i = 1000 + 500 exp(-i / 4) every 5 ms from tick 1000: final = mean of the last 8 = 1000.6056,
        # spread = 499.39 (entry 0), band = 49.94; entry 9 is 52.09 away, entry 10 is 40.44 away, so the
        # first entry inside the band for good is no. 10, at tick 1055: 55 ms
        z = [1000 + 500 * math.exp(-i / 4) + 0j for i in range(32)]
        self.assertEqual(sv.propose_settle(self.trace_entries(z), 1000), 55)
        self.assertIsNone(sv.propose_settle(self.trace_entries(z[:7]), 1000))          # fewer than 8 entries
        self.assertIsNone(sv.propose_settle(self.trace_entries([1000 + 0j] * 32), 1000))   # no transient
        late = [1000 + 0j] * 31 + [1100 + 0j]                  # the last entry is out of the band: unsettled
        self.assertEqual(sv.propose_settle(self.trace_entries(late), 1000), lc.SETTLE_MAX)


if __name__ == "__main__":
    unittest.main()
