"""Tests fuer Parser und Speicherlogik, ohne Netzwerkzugriff.

Das Fragment bildet die am 16.11.2026 beobachtete Struktur von jet.de nach.
Aufruf: python3 -m unittest -v
"""

import tempfile
import unittest
from pathlib import Path

import collector


def price_block(element_id, label, value_html):
    return f"""
<div class="col-lg col-12" id="{element_id}_wrapper">
    <div class="price-table price-padding">
        <div class="row">
            <div class="col-lg-auto col-6 pe-0"><h6 class="lable">{label}</h6></div>
            <div class="col-lg col-6">
                <div id="{element_id}" class="value text-end">
                    {value_html}
                </div>
            </div>
        </div>
    </div>
</div>"""


FRAGMENT = (
    '<div class="accordion-body"><img src="x.svg"><br><div class="row g-3 prices-grid">'
    + price_block("product_price_fuel_diesel", "Diesel", "&euro; 2,11<sup>9</sup>")
    + price_block("product_price_fuel_super", "Super", "&euro; 1,87<sup>9</sup>")
    + price_block("product_price_fuel_super_e10", "Super E10", "&euro; 1,81<sup>9</sup>")
    + price_block("product_price_fuel_super_plus", "Super Plus", "&euro; 1,98<sup>9</sup>")
    + '</div><h6 class="price-status">Stand der Preise:'
    + ' <span id="product_price_timestamp_fuel">16.11.2026, 20:13</span></h6></div>'
)


class ParseTests(unittest.TestCase):
    def test_fragment(self):
        self.assertEqual(
            collector.parse_html(FRAGMENT),
            {
                "price_stand": "2026-11-16T20:13:00+01:00",
                "diesel": "2.119",
                "super": "1.879",
                "super_e10": "1.819",
                "super_plus": "1.989",
            },
        )

    def test_wrapper_is_not_mistaken_for_value(self):
        # Die Wrapper-ID beginnt gleich, darf aber nicht den Preis liefern.
        row = collector.parse_html(FRAGMENT)
        self.assertEqual(row["super"], "1.879")
        self.assertEqual(row["super_e10"], "1.819")

    def test_missing_price_is_empty(self):
        html = price_block("product_price_fuel_diesel", "Diesel", "-")
        row = collector.parse_html(html)
        self.assertEqual(row["diesel"], "")
        self.assertEqual(row["super"], "")
        self.assertEqual(row["price_stand"], "")

    def test_layout_change_raises(self):
        with self.assertRaises(collector.LayoutError):
            collector.parse_html("<html><body><p>Diesel 2,119</p></body></html>")

    def test_price_formats(self):
        self.assertEqual(collector.parse_price("€ 2,119"), "2.119")
        self.assertEqual(collector.parse_price("€ 1,87"), "1.870")
        self.assertEqual(collector.parse_price(""), "")
        with self.assertRaises(ValueError):
            collector.parse_price("€ 9,999")

    def test_stand_summer_time(self):
        self.assertEqual(collector.parse_stand("01.07.2026, 08:05"), "2026-07-01T08:05:00+02:00")

    def test_stand_unreadable_is_kept(self):
        self.assertEqual(collector.parse_stand("gerade eben"), "gerade eben")


class QuietHourTests(unittest.TestCase):
    def utc(self, text):
        from datetime import datetime
        return datetime.fromisoformat(text)

    def test_winter(self):
        # 04:30 UTC = 05:30 MEZ -> Ruhezeit, 05:07 UTC = 06:07 MEZ -> aktiv
        self.assertTrue(collector.in_quiet_hours("0-6", self.utc("2026-11-17T04:30:00+00:00")))
        self.assertFalse(collector.in_quiet_hours("0-6", self.utc("2026-11-17T05:07:00+00:00")))
        self.assertTrue(collector.in_quiet_hours("0-6", self.utc("2026-11-16T23:07:00+00:00")))
        self.assertFalse(collector.in_quiet_hours("0-6", self.utc("2026-11-16T22:52:00+00:00")))

    def test_summer(self):
        # 04:07 UTC = 06:07 MESZ -> aktiv, 22:07 UTC = 00:07 MESZ -> Ruhezeit
        self.assertFalse(collector.in_quiet_hours("0-6", self.utc("2026-07-01T04:07:00+00:00")))
        self.assertTrue(collector.in_quiet_hours("0-6", self.utc("2026-07-01T22:07:00+00:00")))

    def test_disabled_and_wraparound(self):
        self.assertFalse(collector.in_quiet_hours("", self.utc("2026-11-17T02:00:00+00:00")))
        self.assertTrue(collector.in_quiet_hours("22-6", self.utc("2026-11-16T21:30:00+00:00")))


class NotificationTests(unittest.TestCase):
    old = {"diesel": "2.019"}

    def test_crossing_threshold(self):
        title, text, prio = collector.build_notification(self.old, {"diesel": "1.989"}, 1.999, False)
        self.assertEqual(title, "Diesel unter 1,999 €")
        self.assertIn("2,019 € → 1,989 €", text)
        self.assertEqual(prio, 4)

    def test_already_below_no_message(self):
        self.assertIsNone(collector.build_notification({"diesel": "1.989"}, {"diesel": "1.969"}, 1.999, False))

    def test_every_change(self):
        title, _, prio = collector.build_notification(self.old, {"diesel": "2.039"}, 1.999, True)
        self.assertEqual(title, "Diesel gestiegen")
        self.assertEqual(prio, 3)

    def test_no_message_without_change_or_history(self):
        self.assertIsNone(collector.build_notification(self.old, {"diesel": "2.019"}, 1.999, True))
        self.assertIsNone(collector.build_notification(None, {"diesel": "1.5"}, 1.999, True))
        self.assertIsNone(collector.build_notification(self.old, {"diesel": ""}, 1.999, True))

    def test_notify_without_topic_does_nothing(self):
        import os
        os.environ.pop("NTFY_TOPIC", None)
        collector.notify(self.old, {"diesel": "1.0"})  # darf nichts senden und nicht werfen


class StorageTests(unittest.TestCase):
    def test_only_changes_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "prices.csv"
            first = dict(collector.parse_html(FRAGMENT),
                         fetched_utc="2026-11-16T21:07:00Z", source="api")
            self.assertTrue(collector.has_changed(collector.read_last_row(path), first))
            collector.append_row(path, first)

            same = dict(first, fetched_utc="2026-11-16T21:22:00Z", source="page")
            self.assertFalse(collector.has_changed(collector.read_last_row(path), same))

            changed = dict(same, diesel="2.099", price_stand="2026-11-16T22:31:00+01:00")
            self.assertTrue(collector.has_changed(collector.read_last_row(path), changed))
            collector.append_row(path, changed)

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                lines[0], "fetched_utc,price_stand,diesel,super,super_e10,super_plus,source"
            )
            self.assertEqual(len(lines), 3)


if __name__ == "__main__":
    unittest.main()
