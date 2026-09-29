"""
Tests for restocking API endpoints (recommendations and submitted orders).
"""
import json
from datetime import datetime, timedelta

import pytest

import restocking


@pytest.fixture(autouse=True)
def isolated_orders_file(tmp_path, monkeypatch):
    """Point the orders file at a temp dir and start empty, so tests never touch real saved orders."""
    monkeypatch.setattr(restocking, "ORDERS_FILE", str(tmp_path / "restock_orders.json"))
    monkeypatch.setattr(restocking, "_orders", [])


class TestDemandForecastRestockingFields:
    """The forecast data must carry what restocking needs."""

    def test_forecasts_have_restocking_fields(self, client):
        """Every forecast item exposes cost, stock, lead time and supplier."""
        data = client.get("/api/demand").json()
        assert len(data) > 0

        for forecast in data:
            assert forecast["unit_cost"] > 0
            assert forecast["quantity_on_hand"] >= 0
            assert forecast["lead_time_days"] > 0
            assert forecast["supplier"]


class TestRestockingRecommendations:
    """Test suite for GET /api/restocking/recommendations."""

    def test_recommendation_structure(self, client):
        """Response has the summary fields and well-formed items."""
        response = client.get("/api/restocking/recommendations?budget=27500")
        assert response.status_code == 200

        data = response.json()
        for field in ["budget", "total_cost", "remaining_budget", "full_restock_cost",
                      "max_budget", "step", "items", "unfunded"]:
            assert field in data

        item = data["items"][0]
        for field in ["sku", "name", "trend", "supplier", "quantity_on_hand", "forecasted_demand",
                      "shortfall", "recommended_quantity", "unit_cost", "line_cost",
                      "lead_time_days", "fully_covered"]:
            assert field in item

    def test_default_budget_when_omitted(self, client):
        """Without a budget the server picks about half of a full restock and reports the slider range."""
        data = client.get("/api/restocking/recommendations").json()

        assert data["max_budget"] == 55000
        assert data["step"] == 500
        assert data["budget"] == 27500
        assert data["full_restock_cost"] == pytest.approx(54562.2)

    def test_zero_budget_recommends_nothing(self, client):
        """A zero budget buys nothing and every shortfall is left unfunded."""
        data = client.get("/api/restocking/recommendations?budget=0").json()

        assert data["items"] == []
        assert data["total_cost"] == 0
        assert data["remaining_budget"] == 0
        assert len(data["unfunded"]) == 7

    @pytest.mark.parametrize("budget", [1, 100, 1000, 5000, 10000, 27500, 50000, 100000])
    def test_budget_is_never_exceeded(self, client, budget):
        """Total cost stays within budget and the numbers add up."""
        data = client.get(f"/api/restocking/recommendations?budget={budget}").json()

        assert data["total_cost"] <= budget
        assert data["remaining_budget"] == pytest.approx(budget - data["total_cost"])
        assert sum(i["line_cost"] for i in data["items"]) == pytest.approx(data["total_cost"])
        for item in data["items"]:
            assert 0 < item["recommended_quantity"] <= item["shortfall"]
            assert item["line_cost"] == pytest.approx(item["recommended_quantity"] * item["unit_cost"])

    def test_large_budget_covers_everything(self, client):
        """With enough budget every short item is fully covered and nothing is unfunded."""
        data = client.get("/api/restocking/recommendations?budget=1000000").json()

        assert len(data["items"]) == 7
        assert all(i["fully_covered"] for i in data["items"])
        assert data["unfunded"] == []
        assert data["total_cost"] == pytest.approx(data["full_restock_cost"])

    def test_items_ranked_most_urgent_first(self, client):
        """Rising demand first, then stable; within a trend the largest uncovered share first."""
        data = client.get("/api/restocking/recommendations?budget=1000000").json()

        assert [i["sku"] for i in data["items"]] == [
            "WDG-001", "FLT-405", "GSK-203",          # increasing
            "CTL-330", "VLV-506", "SNR-420", "BRG-102"  # stable
        ]

    def test_items_with_enough_stock_never_recommended(self, client):
        """Items whose stock already covers the forecast are left out entirely."""
        data = client.get("/api/restocking/recommendations?budget=1000000").json()

        listed = {i["sku"] for i in data["items"]} | {i["sku"] for i in data["unfunded"]}
        assert "PSU-501" not in listed  # 420 on hand vs 252 forecast
        assert "MTR-304" not in listed  # 60 on hand vs 35 forecast (decreasing demand)

    def test_first_partly_covered_item_takes_the_rest_and_stops(self, client):
        """At 27,500 the three rising items are bought in full, the next item is partly covered, then buying stops."""
        data = client.get("/api/restocking/recommendations?budget=27500").json()

        assert [i["sku"] for i in data["items"]] == ["WDG-001", "FLT-405", "GSK-203", "CTL-330"]
        assert [i["fully_covered"] for i in data["items"]] == [True, True, True, False]

        controller = data["items"][3]
        assert controller["recommended_quantity"] == 41  # 41 x 210.00 is what is left after the first three
        assert data["total_cost"] == pytest.approx(27319.20)
        assert data["remaining_budget"] == pytest.approx(180.80)

        # Items after the partial one are not reached, even though a cheaper one would fit the leftover
        assert [i["sku"] for i in data["unfunded"]] == ["VLV-506", "SNR-420", "BRG-102"]

    def test_unaffordable_item_is_skipped_not_a_stop(self, client):
        """An item the budget cannot buy a single unit of is skipped; a cheaper one further down can still be bought."""
        data = client.get("/api/restocking/recommendations?budget=10").json()

        # WDG-001 costs 24.99 (too much); FLT-405 costs 8.25 (one unit fits)
        assert [i["sku"] for i in data["items"]] == ["FLT-405"]
        assert data["items"][0]["recommended_quantity"] == 1
        assert data["total_cost"] == pytest.approx(8.25)

    def test_negative_budget_rejected(self, client):
        """A negative budget is a validation error."""
        assert client.get("/api/restocking/recommendations?budget=-1").status_code == 422

    def test_non_numeric_budget_rejected(self, client):
        """A budget that is not a number is a validation error."""
        assert client.get("/api/restocking/recommendations?budget=abc").status_code == 422


class TestRestockingOrders:
    """Test suite for POST and GET /api/restocking/orders."""

    def test_no_orders_initially(self, client):
        """Nothing has been submitted yet."""
        response = client.get("/api/restocking/orders")
        assert response.status_code == 200
        assert response.json() == []

    def test_place_order(self, client):
        """Placing an order returns it with totals and delivery lead time."""
        response = client.post("/api/restocking/orders", json={"budget": 27500})
        assert response.status_code == 201

        order = response.json()
        assert order["status"] == "Submitted"
        assert order["order_number"].startswith("RST-")
        assert order["budget"] == 27500
        assert order["total_cost"] == pytest.approx(27319.20)
        assert [i["sku"] for i in order["items"]] == ["WDG-001", "FLT-405", "GSK-203", "CTL-330"]
        assert sum(i["line_cost"] for i in order["items"]) == pytest.approx(order["total_cost"])

    def test_order_lead_time_is_longest_item_lead_time(self, client):
        """The order arrives when its slowest item does, and the delivery date follows from that."""
        order = client.post("/api/restocking/orders", json={"budget": 27500}).json()

        # Item lead times: WDG-001 7, FLT-405 4, GSK-203 5, CTL-330 12
        assert order["lead_time_days"] == 12
        assert order["lead_time_days"] == max(i["lead_time_days"] for i in order["items"])

        submitted = datetime.fromisoformat(order["submitted_at"])
        expected = datetime.fromisoformat(order["expected_delivery"])
        assert expected - submitted == timedelta(days=12)

    def test_submitted_orders_listed_newest_first(self, client):
        """Submitted orders show up in the list, newest first, with increasing order numbers."""
        first = client.post("/api/restocking/orders", json={"budget": 10000}).json()
        second = client.post("/api/restocking/orders", json={"budget": 20000}).json()

        listed = client.get("/api/restocking/orders").json()
        assert [o["id"] for o in listed] == [second["id"], first["id"]]
        assert first["order_number"].endswith("-0001")
        assert second["order_number"].endswith("-0002")

    def test_budget_too_low_to_buy_anything_rejected(self, client):
        """If nothing is affordable the order is refused and nothing is stored."""
        response = client.post("/api/restocking/orders", json={"budget": 5})
        assert response.status_code == 400
        assert "detail" in response.json()
        assert client.get("/api/restocking/orders").json() == []

    def test_negative_budget_rejected(self, client):
        """A negative budget is a validation error."""
        assert client.post("/api/restocking/orders", json={"budget": -5}).status_code == 422

    def test_missing_budget_rejected(self, client):
        """The request must include a budget."""
        assert client.post("/api/restocking/orders", json={}).status_code == 422

    def test_orders_saved_to_file_and_survive_restart(self, client):
        """Orders are written to disk and come back after the in-memory list is lost."""
        order = client.post("/api/restocking/orders", json={"budget": 12000}).json()

        with open(restocking.ORDERS_FILE) as f:
            assert [o["order_number"] for o in json.load(f)] == [order["order_number"]]

        # Simulate a server restart: forget memory, then load from the file like start-up does
        restocking._orders.clear()
        assert client.get("/api/restocking/orders").json() == []
        restocking.load_orders()

        listed = client.get("/api/restocking/orders").json()
        assert [o["order_number"] for o in listed] == [order["order_number"]]

    def test_corrupt_orders_file_is_reported_and_left_alone(self):
        """A damaged file stops start-up with a clear message instead of being silently overwritten."""
        with open(restocking.ORDERS_FILE, "w") as f:
            f.write("{ not json")

        with pytest.raises(RuntimeError, match="not valid JSON"):
            restocking.load_orders()

        with open(restocking.ORDERS_FILE) as f:
            assert f.read() == "{ not json"

    def test_orders_file_with_wrong_shape_is_reported(self):
        """Valid JSON that is not a list of orders is refused at start-up instead of failing every request."""
        with open(restocking.ORDERS_FILE, "w") as f:
            f.write('{"a": 1}')

        with pytest.raises(RuntimeError, match="list of orders"):
            restocking.load_orders()
