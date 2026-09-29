"""
Restocking recommendations and submitted restock orders.

Recommendations are built from the demand forecast data, which carries each item's
unit cost, stock on hand and supplier lead time. Submitted orders are kept in memory
and saved to a JSON file so they survive a server restart.
"""
import json
import math
import os
import tempfile
import threading
from datetime import datetime, timedelta

from mock_data import demand_forecasts, DATA_DIR

# Runtime file (gitignored): created when the first order is submitted.
ORDERS_FILE = os.path.join(DATA_DIR, 'restock_orders.json')

# Slider granularity for the budget, in dollars.
BUDGET_STEP = 500
# The slider maximum is the full restock cost rounded up to a multiple of this.
MAX_BUDGET_ROUNDING = 1000

# Lower number = more urgent.
TREND_PRIORITY = {'increasing': 0, 'stable': 1, 'decreasing': 2}

_lock = threading.Lock()
_orders = []  # submitted orders, oldest first


def load_orders():
    """Replace the in-memory orders with the contents of ORDERS_FILE (empty if it does not exist)."""
    global _orders
    try:
        with open(ORDERS_FILE, 'r') as f:
            loaded = json.load(f)
    except FileNotFoundError:
        _orders = []
        return
    except json.JSONDecodeError as e:
        # Refuse to start on purpose: carrying on with an empty list would overwrite
        # the saved orders on the next submission. Say which file to look at.
        raise RuntimeError(
            f"Cannot read saved restock orders: {ORDERS_FILE} is not valid JSON ({e}). "
            "Fix or move the file, then start the server again."
        ) from e

    # Valid JSON of the wrong shape (for example an object) would not fail here but on
    # every later request, so it is rejected at start-up with the same message style.
    if not isinstance(loaded, list):
        raise RuntimeError(
            f"Cannot read saved restock orders: {ORDERS_FILE} should contain a list of orders. "
            "Fix or move the file, then start the server again."
        )
    _orders = loaded


def _write_orders(orders):
    """Save orders to ORDERS_FILE without ever leaving a half-written file behind."""
    # The temp file lives in the same directory so os.replace is an atomic rename.
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(ORDERS_FILE), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(orders, f, indent=2)
        os.replace(tmp_path, ORDERS_FILE)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _candidates():
    """Forecast items that will run short of stock, most urgent first."""
    items = []
    for f in demand_forecasts:
        shortfall = max(0, f['forecasted_demand'] - f['quantity_on_hand'])
        if shortfall == 0:
            continue  # enough stock: never recommended (also avoids dividing by a zero forecast below)
        items.append({
            'sku': f['item_sku'],
            'name': f['item_name'],
            'trend': f['trend'],
            'supplier': f['supplier'],
            'quantity_on_hand': f['quantity_on_hand'],
            'forecasted_demand': f['forecasted_demand'],
            'shortfall': shortfall,
            'unit_cost': f['unit_cost'],
            'lead_time_days': f['lead_time_days'],
            # Share of forecasted demand that current stock cannot cover.
            'gap_ratio': shortfall / f['forecasted_demand'],
        })

    # "Most urgent first": rising demand beats stable beats falling; within a trend,
    # the item with the largest uncovered share of its forecast goes first.
    items.sort(key=lambda i: (TREND_PRIORITY.get(i['trend'], len(TREND_PRIORITY)), -i['gap_ratio'], i['sku']))
    return items


def _to_cents(amount):
    return round(amount * 100)


def get_full_restock_cost():
    """Cost of covering every item's full shortfall."""
    return sum(c['shortfall'] * _to_cents(c['unit_cost']) for c in _candidates()) / 100


def get_max_budget():
    """Slider maximum: the full restock cost rounded up, so the top of the slider always covers everything."""
    rounded = math.ceil(get_full_restock_cost() / MAX_BUDGET_ROUNDING) * MAX_BUDGET_ROUNDING
    return max(BUDGET_STEP, int(rounded))


def build_recommendations(budget=None):
    """
    Recommend what to restock for a budget (in dollars).

    Items are bought in priority order. An item that fits the remaining budget is
    bought in full. The first item that can only be partly afforded gets whatever
    the budget still allows, and buying stops there. An item the budget cannot cover
    even one unit of is skipped, since a cheaper item further down may still fit.
    """
    max_budget = get_max_budget()
    if budget is None:
        # Default the slider to about half of what a full restock costs.
        budget = (max_budget // 2 // BUDGET_STEP) * BUDGET_STEP

    # All money maths is done in whole cents so sums never drift by a fraction of a cent.
    remaining_cents = _to_cents(budget)
    spent_cents = 0
    funded = []
    unfunded = []
    stopped = False

    for c in _candidates():
        if stopped:
            unfunded.append(_unfunded_entry(c))
            continue

        unit_cents = _to_cents(c['unit_cost'])
        if c['shortfall'] * unit_cents <= remaining_cents:
            quantity = c['shortfall']
        else:
            quantity = remaining_cents // unit_cents
            if quantity > 0:
                stopped = True  # partly covered: this item takes the rest of the budget

        if quantity == 0:
            unfunded.append(_unfunded_entry(c))
            continue

        line_cents = quantity * unit_cents
        remaining_cents -= line_cents
        spent_cents += line_cents
        funded.append({
            'sku': c['sku'],
            'name': c['name'],
            'trend': c['trend'],
            'supplier': c['supplier'],
            'quantity_on_hand': c['quantity_on_hand'],
            'forecasted_demand': c['forecasted_demand'],
            'shortfall': c['shortfall'],
            'recommended_quantity': quantity,
            'unit_cost': c['unit_cost'],
            'line_cost': line_cents / 100,
            'lead_time_days': c['lead_time_days'],
            'fully_covered': quantity == c['shortfall'],
        })

    return {
        'budget': budget,
        'total_cost': spent_cents / 100,
        'remaining_budget': remaining_cents / 100,
        'full_restock_cost': get_full_restock_cost(),
        'max_budget': max_budget,
        'step': BUDGET_STEP,
        'items': funded,
        'unfunded': unfunded,
    }


def _unfunded_entry(candidate):
    return {
        'sku': candidate['sku'],
        'name': candidate['name'],
        'trend': candidate['trend'],
        'supplier': candidate['supplier'],
        'shortfall': candidate['shortfall'],
        'unit_cost': candidate['unit_cost'],
    }


def submit_order(budget):
    """
    Place a restock order for the recommendation at this budget and save it.

    The recommendation is rebuilt here from the server's own data, so the client only
    ever sends a budget and cannot influence prices or quantities.
    Raises ValueError if the budget cannot buy anything.
    """
    recommendation = build_recommendations(budget)
    if not recommendation['items']:
        raise ValueError('The budget is too low to buy any recommended item')

    now = datetime.now()
    # The order is only complete when its slowest item arrives, so the order's
    # lead time is the longest lead time among its items.
    lead_time_days = max(item['lead_time_days'] for item in recommendation['items'])

    with _lock:
        sequence = len(_orders) + 1  # orders are never deleted, so this stays unique
        order = {
            'id': str(sequence),
            'order_number': f"RST-{now.year}-{sequence:04d}",
            'submitted_at': now.isoformat(timespec='seconds'),
            'budget': budget,
            'total_cost': recommendation['total_cost'],
            'status': 'Submitted',
            'lead_time_days': lead_time_days,
            'expected_delivery': (now + timedelta(days=lead_time_days)).isoformat(timespec='seconds'),
            'items': [{
                'sku': item['sku'],
                'name': item['name'],
                'supplier': item['supplier'],
                'quantity': item['recommended_quantity'],
                'unit_cost': item['unit_cost'],
                'line_cost': item['line_cost'],
                'lead_time_days': item['lead_time_days'],
            } for item in recommendation['items']],
        }
        # Save first: if writing fails the order is not kept in memory either,
        # so memory and file never disagree.
        _write_orders(_orders + [order])
        _orders.append(order)
    return order


def list_orders():
    """Submitted orders, newest first."""
    with _lock:
        return list(reversed(_orders))


load_orders()
