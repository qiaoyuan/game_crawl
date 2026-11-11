import unittest

from g2g.coin_detail import parse_coin_detail
from g2g.coin_offers import CoinApiUnavailable


class CoinDetailTests(unittest.TestCase):
    def setUp(self):
        self.listing = {
            "offer_id": "offer-1", "seller_id": "12", "username": "Example",
            "region_id": "r1", "relation_id": "market-1",
            "offer_group_attributes": {"zone": ["one"]}, "display_price": "0.32",
        }
        self.detail = {
            **self.listing, "status": "live", "is_sold_out": False, "is_expired": False,
            "offer_type": "public", "is_supported_locally": True,
            "min_qty": 3, "available_qty": 100, "unit_quantity": 1, "is_subscription": False,
            "pricing_packages": None, "package_settings": None, "wholesale_details": [],
            "other_wholesale_details": [], "other_pricing": [], "display_currency": "USD",
            "offer_currency": "EUR", "unit_price": 0.3, "unit_price_in_usd": 0.315,
            "display_price": "0.32001270", "converted_unit_price": 0.3200127,
        }

    def parse(self, **changes):
        return parse_coin_detail({"code": 2000, "payload": {**self.detail, **changes}}, self.listing)

    def test_uses_full_precision_from_detail(self):
        self.assertEqual(self.parse(), "0.32001270")
        self.assertEqual(self.parse(offer_currency="USD", unit_price=0.3200127), "0.32001270")

    def test_rejects_different_offer_seller_or_market(self):
        for key, value in [("offer_id", "offer-2"), ("seller_id", "34"), ("username", "Other"),
                           ("region_id", "r2"), ("relation_id", "market-2"),
                           ("offer_group_attributes", {"zone": ["two"]})]:
            with self.subTest(key=key), self.assertRaises(CoinApiUnavailable):
                self.parse(**{key: value})

    def test_ambiguous_price_and_checkout_require_page(self):
        for changes in (
                {"display_price": "NaN"}, {"display_price": "0"}, {"converted_unit_price": None},
                {"display_currency": "EUR"}, {"offer_currency": "USD"}, {"offer_currency": ""},
                {"converted_unit_price": 0.32}, {"other_pricing": [{"currency": "USD"}]},
                {"min_qty": 1}, {"min_qty": "3"}, {"available_qty": 2}, {"unit_quantity": True},
                {"wholesale_details": [{"min": 2, "max": 10, "unit_price": 0.1}]},
                {"other_wholesale_details": [{"currency": "USD"}]}, {"pricing_packages": {}},
                {"package_settings": {"type": "quantity"}}, {"is_subscription": True},
                {"is_sold_out": True}, {"is_expired": True}, {"status": "inactive"},
                {"is_supported_locally": False}, {"offer_type": "private"}):
            with self.subTest(changes=changes), self.assertRaises(CoinApiUnavailable):
                self.parse(**changes)

    def test_rejects_incomplete_responses(self):
        for response in (None, {"code": 2000, "payload": []}, {"code": 4040, "payload": self.detail}):
            with self.subTest(response=response), self.assertRaises(CoinApiUnavailable):
                parse_coin_detail(response, self.listing)
        for field in ("offer_id", "converted_unit_price", "pricing_packages", "wholesale_details"):
            incomplete = self.detail.copy()
            del incomplete[field]
            with self.subTest(field=field), self.assertRaises(CoinApiUnavailable):
                parse_coin_detail({"code": 2000, "payload": incomplete}, self.listing)
