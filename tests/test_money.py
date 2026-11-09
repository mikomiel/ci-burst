import unittest

from ci_burst.money import format_money, hourly_cost_micros, parse_money


class ParseMoneyTests(unittest.TestCase):
    def test_exact_decimal(self):
        self.assertEqual(parse_money("0.0425", "price"), 42_500)
        self.assertEqual(parse_money("10", "price"), 10_000_000)
        self.assertEqual(parse_money("0.000001", "price"), 1)

    def test_rejects_values_that_would_round(self):
        for value in ("0.0000001", "1.2345678"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_money(value, "price")

    def test_rejects_non_decimal_input(self):
        for value in (0.03, 3, "1e3", "-1", " 1.00", "1.0 ", "", ".5", "1."):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_money(value, "price")

    def test_rejects_zero(self):
        with self.assertRaises(ValueError):
            parse_money("0.000000", "price")


class HourlyCostTests(unittest.TestCase):
    def test_whole_hours_are_exact(self):
        self.assertEqual(hourly_cost_micros(40_000, 3600), 40_000)
        self.assertEqual(hourly_cost_micros(40_000, 7200), 80_000)

    def test_partial_hours_round_up_to_one_micro(self):
        self.assertEqual(hourly_cost_micros(40_000, 60), 667)
        self.assertEqual(hourly_cost_micros(1, 1), 1)

    def test_zero_duration(self):
        self.assertEqual(hourly_cost_micros(40_000, 0), 0)

    def test_negative_refused(self):
        with self.assertRaises(ValueError):
            hourly_cost_micros(-1, 60)


class FormatMoneyTests(unittest.TestCase):
    def test_display_only(self):
        self.assertEqual(format_money(42_500), "0.042500")
        self.assertEqual(format_money(10_000_000), "10.000000")
