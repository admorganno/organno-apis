import importlib
import os
import sys
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch


os.environ.setdefault("NUMMUS_API_KEY", "test-api-key")
os.environ.setdefault("NUMMUS_CLIENT_ID", "test-client-id")
sys.modules.setdefault("requests", SimpleNamespace())

main = importlib.import_module("main")


def make_cashback(
    *,
    cashback_id: str,
    document_number: str,
    name: str,
    dh_operation: str,
    expires_in: int,
    value_cashback: float,
    value_rescued: float = 0.0,
) -> dict:
    return {
        "id": cashback_id,
        "dh_operation": dh_operation,
        "value_cashback": value_cashback,
        "value_rescued": value_rescued,
        "customer": {
            "document_number": document_number,
            "name": name,
        },
        "products": [
            {
                "expiresIn": expires_in,
            }
        ],
    }


class BuildCustomerExpiryTargetsTests(unittest.TestCase):
    def test_returns_only_customers_with_total_active_balance_above_threshold(self) -> None:
        today = date(2026, 5, 6)
        cashbacks = [
            make_cashback(
                cashback_id="cb-15",
                document_number="111",
                name="Cliente 15 dias",
                dh_operation="06/04/2026 10:00",
                expires_in=45,
                value_cashback=25.0,
            ),
            make_cashback(
                cashback_id="cb-active-extra",
                document_number="111",
                name="Cliente 15 dias",
                dh_operation="01/05/2026 10:00",
                expires_in=20,
                value_cashback=10.0,
            ),
            make_cashback(
                cashback_id="cb-3",
                document_number="222",
                name="Cliente 3 dias",
                dh_operation="29/04/2026 10:00",
                expires_in=10,
                value_cashback=32.0,
            ),
            make_cashback(
                cashback_id="cb-below-threshold",
                document_number="333",
                name="Cliente Ignorado",
                dh_operation="06/04/2026 10:00",
                expires_in=45,
                value_cashback=29.0,
            ),
        ]

        with patch.object(main, "fetch_all_cashbacks", return_value=cashbacks):
            events = main.build_customer_expiry_targets(today)

        self.assertEqual(len(events), 2)

        events_by_doc = {event["document_number"]: event for event in events}

        self.assertEqual(events_by_doc["111"]["periodo"], "15 dias")
        self.assertEqual(events_by_doc["111"]["saldo_total"], 35.0)
        self.assertEqual(events_by_doc["111"]["value_to_expire"], 35.0)
        self.assertEqual(events_by_doc["111"]["cashback_count"], 2)

        self.assertEqual(events_by_doc["222"]["periodo"], "3 dias")
        self.assertEqual(events_by_doc["222"]["saldo_total"], 32.0)
        self.assertEqual(events_by_doc["222"]["value_to_expire"], 32.0)
        self.assertEqual(events_by_doc["222"]["cashback_count"], 1)

        self.assertNotIn("333", events_by_doc)

    def test_prioritizes_3_day_window_over_15_day_window_for_same_customer(self) -> None:
        today = date(2026, 5, 6)
        cashbacks = [
            make_cashback(
                cashback_id="cb-15",
                document_number="111",
                name="Cliente Prioridade",
                dh_operation="06/04/2026 10:00",
                expires_in=45,
                value_cashback=20.0,
            ),
            make_cashback(
                cashback_id="cb-3",
                document_number="111",
                name="Cliente Prioridade",
                dh_operation="29/04/2026 10:00",
                expires_in=10,
                value_cashback=15.0,
            ),
        ]

        with patch.object(main, "fetch_all_cashbacks", return_value=cashbacks):
            events = main.build_customer_expiry_targets(today)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["document_number"], "111")
        self.assertEqual(events[0]["periodo"], "3 dias")
        self.assertEqual(events[0]["dias_para_expirar"], 3)
        self.assertEqual(events[0]["saldo_total"], 35.0)
        self.assertEqual(events[0]["value_to_expire"], 15.0)
        self.assertEqual(events[0]["cashback_count"], 1)


if __name__ == "__main__":
    unittest.main()
