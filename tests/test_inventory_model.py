import math
import unittest

from strategy.inventory_model import PriceVariance, _log_value_vector, inventory_quotes


class InventoryModelTests(unittest.TestCase):
    def quote(self, **kwargs):
        args = dict(model="glft", mid=.5, inventory=0, lot_size=1, max_inventory=10,
                    gamma=.1, sigma=.02, k=100, arrival_rate=.5, horizon=60)
        args.update(kwargs)
        return inventory_quotes(**args)

    def test_as_paper_equations_and_inventory_shift(self):
        # Paper simulation parameters; translation of s does not change deltas.
        quote = self.quote(model="as", inventory=2, gamma=.1, sigma=2, k=1.5, horizon=1)
        center = .5 - 2 * .1 * 4
        half = .1 * 4 / 2 + math.log1p(.1 / 1.5) / .1
        self.assertAlmostEqual(quote.bid, center - half)
        self.assertAlmostEqual(quote.ask, center + half)

    def test_glft_terminal_quotes(self):
        quote = self.quote(inventory=3, horizon=0)
        half = math.log1p(.1 / 100) / .1
        self.assertAlmostEqual(quote.bid, .5 - half)
        self.assertAlmostEqual(quote.ask, .5 + half)

    def test_glft_q1_matrix_exponential_against_analytic_solution(self):
        # Q=1, sigma=0: v0=cosh(z)+sqrt(2)*sinh(z),
        # v+-1=cosh(z)+sinh(z)/sqrt(2), z=sqrt(2)*eta*tau.
        eta, tau = .3, 2
        logs = _log_value_vector(1, 0, eta, tau)
        z = math.sqrt(2) * eta * tau
        expected_ratio = ((math.cosh(z) + math.sinh(z) / math.sqrt(2)) /
                          (math.cosh(z) + math.sqrt(2) * math.sinh(z)))
        self.assertAlmostEqual(math.exp(logs[0] - logs[1]), expected_ratio, places=12)
        self.assertAlmostEqual(logs[0], logs[2], places=12)

    def test_glft_short_horizon_matches_as_expansion(self):
        # Far from inventory bounds, GLFT's Taylor expansion is A-S.
        glft = self.quote(inventory=2, horizon=.0001)
        approx = self.quote(model="as", inventory=2, horizon=.0001)
        self.assertAlmostEqual(glft.bid, approx.bid, places=10)
        self.assertAlmostEqual(glft.ask, approx.ask, places=10)

    def test_glft_nonzero_risk_against_analytic_q1_solution(self):
        alpha, eta, tau = .4, .3, 2
        logs = _log_value_vector(1, alpha, eta, tau)
        d = math.sqrt(alpha**2 / 4 + 2 * eta**2)
        edge = math.cosh(d*tau) + (eta-alpha/2)/d * math.sinh(d*tau)
        middle = math.cosh(d*tau) + (2*eta+alpha/2)/d * math.sinh(d*tau)
        self.assertAlmostEqual(math.exp(logs[0]-logs[1]), edge/middle, places=12)

    def test_inventory_symmetry_and_direction(self):
        for model in ("as", "glft"):
            neutral = self.quote(model=model)
            long = self.quote(model=model, inventory=3)
            short = self.quote(model=model, inventory=-3)
            self.assertLess(long.bid, neutral.bid)
            self.assertLess(long.ask, neutral.ask)
            self.assertAlmostEqual(long.bid, 1 - short.ask)
            self.assertAlmostEqual(long.ask, 1 - short.bid)

    def test_lot_scaling_preserves_as_share_risk(self):
        q = self.quote(model="as", inventory=8, lot_size=8, max_inventory=50)
        risk = .1 * .02**2 * 60
        base = math.log1p(.1 * 8 / 100) / (.1 * 8)
        self.assertAlmostEqual((q.bid + q.ask) / 2, .5 - 8 * risk)
        self.assertAlmostEqual((q.ask - q.bid) / 2, base + 8 * risk / 2)

    def test_glft_bounds_and_fractional_inventory(self):
        self.assertIsNone(self.quote(inventory=10).bid)
        self.assertIsNone(self.quote(inventory=-10).ask)
        self.assertIsNotNone(self.quote(inventory=50).ask)
        self.assertIsNone(self.quote(inventory=9.5).bid)
        left, right = self.quote(inventory=2), self.quote(inventory=3)
        partial = self.quote(inventory=2.5)
        self.assertAlmostEqual(partial.ask, (left.ask + right.ask) / 2)

    def test_long_horizon_and_large_arrival_rate_remain_finite(self):
        q = self.quote(arrival_rate=50, horizon=3600, inventory=5)
        self.assertTrue(math.isfinite(q.bid) and math.isfinite(q.ask))
        self.assertLess(q.bid, q.ask)

    def test_invalid_inputs_fail_closed(self):
        for kwargs in ({"sigma": float("nan")}, {"gamma": 0}, {"k": -1},
                       {"horizon": -1}, {"lot_size": 11}, {"model": "typo"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.quote(**kwargs)

    def test_variance_uses_absolute_moves_and_elapsed_seconds(self):
        v = PriceVariance(10, 3, .001)
        self.assertEqual(v.observe(1, .5), .001)
        v.observe(2, .51)
        self.assertAlmostEqual(v.observe(3, .50), .01)
        self.assertAlmostEqual(v.observe(3, .90), .01)  # duplicate snapshot ignored
        self.assertEqual(v.observe(20, .9), .001)  # old observations expire


if __name__ == "__main__":
    unittest.main()
