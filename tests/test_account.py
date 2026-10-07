"""Time-account parsing against synthetic rows of the current portal layout."""

import unittest

from hrworks_worker.portal import account_metrics, minutes

# Section titles are single cells and never reach the parser; only value rows do.
CURRENT_MONTH = [
    ["Target hours", "100:00\xa0Hours"],
    ["Time credit", "0:00\xa0Hours"],
    ["Recorded hours (Working hours)", "21:10\xa0(20:50)"],
    ["End of month balance", "-79:10\xa0Hours"],
    ["Difference", "0:00\xa0Hours"],
    ["End of month balance", "-79:10\xa0Hours"],
    ["Transferred\xa0from\xa0previous\xa0month", "02:00\xa0Hours"],
    ["Total balance", "-77:10\xa0Hours"],
    ["Monthly balance to the previous day", "01:05\xa0Hours"],
    ["Transferred\xa0from\xa0previous\xa0month", "02:00\xa0Hours"],
    ["Total balance to the previous day", "03:05\xa0Hours"],
]


class AccountTests(unittest.TestCase):
    def test_current_layout_uses_credited_hours_and_previous_day(self):
        metrics, basis = account_metrics(CURRENT_MONTH)
        self.assertEqual(basis, "previous_day")
        self.assertEqual(
            metrics,
            {
                "monthly_target": 6000,
                "time_credit": 0,
                "monthly_worked": 1250,
                "carryover": 120,
                "monthly_balance": 65,
                "total_balance": 185,
            },
        )

    def test_missing_rows_stay_absent_instead_of_zero(self):
        rows = [r for r in CURRENT_MONTH if not r[0].startswith(("Recorded", "Monthly"))]
        metrics, basis = account_metrics(rows)
        self.assertEqual(basis, "previous_day")
        self.assertNotIn("monthly_worked", metrics)
        self.assertNotIn("absence_deduction", metrics)
        # End-of-month projections must not stand in for a previous-day balance.
        self.assertNotIn("monthly_balance", metrics)
        self.assertEqual(metrics["total_balance"], 185)

    def test_end_of_month_basis_without_previous_day_section(self):
        metrics, basis = account_metrics(CURRENT_MONTH[:8])
        self.assertEqual(basis, "end_of_month")
        self.assertEqual(metrics["monthly_balance"], -4750)
        self.assertEqual(metrics["total_balance"], -4630)

    def test_unparseable_values_are_skipped(self):
        metrics, _ = account_metrics([["Target hours", "n/a"], ["Total balance", "--:--"]])
        self.assertEqual(metrics, {})

    def test_legacy_working_hours_row(self):
        metrics, _ = account_metrics([["Working hours", "10:00 Hours"]])
        self.assertEqual(metrics, {"monthly_worked": 600})

    def test_unicode_minus(self):
        self.assertEqual(minutes("−1:30"), -90)
        self.assertEqual(minutes("1:30"), 90)
