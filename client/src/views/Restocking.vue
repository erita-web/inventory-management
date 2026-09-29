<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="!recommendation" class="error">{{ t('restocking.loadFailed') }}</div>
    <div v-else>
      <!-- Refresh failures keep the last good data on screen instead of replacing the page -->
      <div v-if="loadFailed" class="error">{{ t('restocking.loadFailed') }}</div>

      <div class="card budget-card">
        <div class="budget-top">
          <div>
            <label class="budget-label" for="budget-slider">{{ t('restocking.budget') }}</label>
            <div class="budget-value">{{ formatWhole(budget) }}</div>
          </div>
          <div class="full-cost">
            {{ t('restocking.fullRestockCost') }}: {{ formatWhole(recommendation.full_restock_cost) }}
          </div>
        </div>
        <input
          id="budget-slider"
          class="budget-slider"
          type="range"
          min="0"
          :max="recommendation.max_budget"
          :step="recommendation.step"
          :value="budget"
          :aria-valuetext="formatWhole(budget)"
          @input="onBudgetInput"
        />
        <div class="budget-range">
          <span>{{ formatWhole(0) }}</span>
          <span>{{ formatWhole(recommendation.max_budget) }}</span>
        </div>
        <p class="budget-help">{{ t('restocking.budgetHelp') }}</p>
      </div>

      <div :class="['results', { 'is-pending': pending }]">
        <div class="stats-grid">
          <div class="stat-card">
            <div class="stat-label">{{ t('restocking.budget') }}</div>
            <div class="stat-value">{{ formatWhole(recommendation.budget) }}</div>
          </div>
          <div class="stat-card info">
            <div class="stat-label">{{ t('restocking.recommendedSpend') }}</div>
            <div class="stat-value">{{ formatMoney(recommendation.total_cost) }}</div>
          </div>
          <div class="stat-card success">
            <div class="stat-label">{{ t('restocking.remainingBudget') }}</div>
            <div class="stat-value">{{ formatMoney(recommendation.remaining_budget) }}</div>
          </div>
          <div class="stat-card">
            <div class="stat-label">{{ t('restocking.itemsToOrder') }}</div>
            <div class="stat-value">{{ items.length }}</div>
          </div>
        </div>

        <div class="card">
          <div class="card-header">
            <h3 class="card-title">{{ t('restocking.recommendedItems') }} ({{ items.length }})</h3>
            <div class="card-total">
              {{ t('restocking.recommendedSpend') }}:
              <strong>{{ formatMoney(recommendation.total_cost) }}</strong>
            </div>
          </div>

          <div v-if="items.length === 0" class="empty-state">
            {{ t('restocking.noRecommendations') }}
          </div>
          <div v-else class="table-container">
            <table>
              <thead>
                <tr>
                  <th>{{ t('restocking.table.item') }}</th>
                  <th>{{ t('restocking.table.supplier') }}</th>
                  <th>{{ t('restocking.table.trend') }}</th>
                  <th class="num">{{ t('restocking.table.onHand') }}</th>
                  <th class="num">{{ t('restocking.table.forecast') }}</th>
                  <th class="num">{{ t('restocking.table.orderQty') }}</th>
                  <th class="num">{{ t('restocking.table.unitCost') }}</th>
                  <th class="num">{{ t('restocking.table.lineCost') }}</th>
                  <th>{{ t('restocking.table.leadTime') }}</th>
                  <th>{{ t('restocking.table.coverage') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in items" :key="item.sku">
                  <td><strong>{{ translateProductName(item.name) }}</strong></td>
                  <td>{{ item.supplier }}</td>
                  <td>
                    <span :class="['badge', item.trend]">{{ t(`trends.${item.trend}`) }}</span>
                  </td>
                  <td class="num">{{ item.quantity_on_hand }}</td>
                  <td class="num">{{ item.forecasted_demand }}</td>
                  <td class="num"><strong>{{ item.recommended_quantity }}</strong></td>
                  <td class="num">{{ formatMoney(item.unit_cost) }}</td>
                  <td class="num"><strong>{{ formatMoney(item.line_cost) }}</strong></td>
                  <td>{{ t('restocking.days', { count: item.lead_time_days }) }}</td>
                  <td>
                    <span :class="['badge', item.fully_covered ? 'success' : 'warning']">
                      {{ item.fully_covered ? t('restocking.fullyCovered') : t('restocking.partial') }}
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="order-footer">
            <div v-if="placedOrder" class="order-banner success">
              <span>
                {{ t('restocking.orderPlaced', {
                  orderNumber: placedOrder.order_number,
                  days: placedOrder.lead_time_days
                }) }}
              </span>
              <router-link to="/orders" class="order-link">{{ t('restocking.viewInOrders') }}</router-link>
            </div>
            <div v-if="orderError" class="order-banner failure">
              {{ t('restocking.orderFailed', { message: orderError }) }}
            </div>
            <button
              type="button"
              class="place-order-btn"
              :disabled="!canPlaceOrder"
              @click="placeOrder"
            >
              {{ submitting ? t('restocking.placingOrder') : t('restocking.placeOrder') }}
            </button>
          </div>
        </div>

        <div v-if="unfunded.length > 0" class="unfunded">
          <h3 class="unfunded-title">{{ t('restocking.notFunded') }} ({{ unfunded.length }})</h3>
          <p class="unfunded-help">{{ t('restocking.notFundedHelp') }}</p>
          <ul class="unfunded-list">
            <li v-for="item in unfunded" :key="item.sku" class="unfunded-item">
              <span class="unfunded-name">{{ translateProductName(item.name) }}</span>
              <span class="unfunded-meta">
                {{ t('restocking.shortBy', { qty: item.shortfall, price: formatMoney(item.unit_cost) }) }}
              </span>
            </li>
          </ul>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'
import { formatCurrency, formatCurrencyWithDecimals } from '../utils/currency'

// Wait this long after the last slider movement before asking the server
const DEBOUNCE_MS = 300

export default {
  name: 'Restocking',
  setup() {
    const { t, currentCurrency, translateProductName } = useI18n()

    const loading = ref(true)
    const loadFailed = ref(false)
    const recommendation = ref(null)

    // Always USD; only the displayed label is converted to yen. The backend
    // works in USD, so converting the stored value would corrupt what we submit.
    const budget = ref(0)

    // True from the moment the slider moves (including the debounce wait) until
    // the matching response arrives, so the order button can never submit a
    // recommendation that no longer matches the slider.
    const pending = ref(false)
    const submitting = ref(false)
    const placedOrder = ref(null)
    const orderError = ref(null)

    let debounceTimer = null
    // Incremented per request; responses from older requests are ignored
    let latestRequestId = 0

    const items = computed(() => recommendation.value?.items ?? [])
    const unfunded = computed(() => recommendation.value?.unfunded ?? [])

    // After a successful order the button stays off until the budget changes
    // (moving the slider clears placedOrder), so a second click cannot create a duplicate order
    const canPlaceOrder = computed(() => {
      return items.value.length > 0 && !pending.value && !submitting.value &&
        !loadFailed.value && !placedOrder.value
    })

    // Budget and full-cost figures are whole-dollar; everything else can carry cents
    // (unit costs like 12.75), so it is shown with two decimals in USD.
    const formatWhole = (amount) => formatCurrency(amount, currentCurrency.value)
    const formatMoney = (amount) => formatCurrencyWithDecimals(amount, currentCurrency.value, 2)

    const loadRecommendations = async (requestedBudget) => {
      const requestId = ++latestRequestId
      pending.value = true

      try {
        const data = await api.getRestockingRecommendations(requestedBudget)
        // Dragging fires several requests that can finish out of order; only
        // the newest one may update the page
        if (requestId !== latestRequestId) return

        recommendation.value = data
        // With no budget the server picks a default, so the slider starts there.
        // Later responses must not move the slider, the user may have dragged on.
        if (requestedBudget === undefined) budget.value = data.budget
        loadFailed.value = false
      } catch (err) {
        if (requestId !== latestRequestId) return
        loadFailed.value = true
        console.error('Failed to load restocking recommendations:', err)
      } finally {
        if (requestId === latestRequestId) {
          pending.value = false
          loading.value = false
        }
      }
    }

    const onBudgetInput = (event) => {
      // Update the label right away; only the server call is debounced
      budget.value = Number(event.target.value)
      pending.value = true
      // A result message about the previous budget would be misleading now
      placedOrder.value = null
      orderError.value = null

      // A request already in flight is for the old slider position; invalidating it
      // keeps the page "pending" until the response for the new position arrives
      latestRequestId++
      clearTimeout(debounceTimer)
      debounceTimer = setTimeout(() => loadRecommendations(budget.value), DEBOUNCE_MS)
    }

    const placeOrder = async () => {
      if (!canPlaceOrder.value) return

      submitting.value = true
      placedOrder.value = null
      orderError.value = null

      try {
        // Submit the budget the visible table was built for (the server may
        // have adjusted it), not the raw slider value
        placedOrder.value = await api.submitRestockingOrder(recommendation.value.budget)
      } catch (err) {
        const detail = err.response?.data?.detail
        // Validation errors (422) return a list instead of text, so fall back to the generic message
        orderError.value = typeof detail === 'string' ? detail : err.message
      } finally {
        submitting.value = false
      }
    }

    onMounted(() => loadRecommendations())

    // Avoid firing a request after the user has left the page
    onBeforeUnmount(() => clearTimeout(debounceTimer))

    return {
      t,
      translateProductName,
      loading,
      loadFailed,
      recommendation,
      budget,
      pending,
      submitting,
      placedOrder,
      orderError,
      items,
      unfunded,
      canPlaceOrder,
      formatWhole,
      formatMoney,
      onBudgetInput,
      placeOrder
    }
  }
}
</script>

<style scoped>
.budget-top {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin-bottom: 1rem;
}

.budget-label {
  display: block;
  color: #64748b;
  font-size: 0.875rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 0.375rem;
}

.budget-value {
  font-size: 2.5rem;
  font-weight: 700;
  color: #0f172a;
  letter-spacing: -0.025em;
  line-height: 1.1;
}

.full-cost {
  font-size: 0.813rem;
  color: #64748b;
}

.budget-slider {
  width: 100%;
  height: 1.5rem;
  cursor: pointer;
  accent-color: #2563eb;
}

.budget-range {
  display: flex;
  justify-content: space-between;
  font-size: 0.813rem;
  color: #64748b;
  margin-top: 0.25rem;
}

.budget-help {
  margin-top: 0.75rem;
  font-size: 0.875rem;
  color: #64748b;
}

/* Dim the old numbers while new ones are on the way */
.results {
  transition: opacity 0.2s ease;
}

.results.is-pending {
  opacity: 0.55;
}

.card-total {
  font-size: 0.875rem;
  color: #64748b;
}

.card-total strong {
  color: #0f172a;
  font-size: 1rem;
}

th.num,
td.num {
  text-align: right;
  white-space: nowrap;
}

.empty-state {
  text-align: center;
  padding: 2rem;
  color: #64748b;
  font-size: 0.938rem;
}

.order-footer {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 0.75rem;
  margin-top: 1rem;
  padding-top: 1rem;
  border-top: 1px solid #e2e8f0;
}

.order-banner {
  align-self: stretch;
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.75rem;
  padding: 0.875rem 1rem;
  border-radius: 8px;
  font-size: 0.938rem;
}

.order-banner.success {
  background: #d1fae5;
  border: 1px solid #a7f3d0;
  color: #065f46;
}

.order-banner.failure {
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: #991b1b;
}

.order-link {
  color: #065f46;
  font-weight: 600;
  text-decoration: underline;
}

.order-link:hover {
  color: #064e3b;
}

.place-order-btn {
  padding: 0.75rem 1.75rem;
  background: #2563eb;
  color: white;
  border: none;
  border-radius: 8px;
  font-size: 0.938rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s ease, opacity 0.2s ease;
  white-space: nowrap;
}

.place-order-btn:hover:not(:disabled) {
  background: #1d4ed8;
}

.place-order-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.unfunded {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  padding: 1rem 1.25rem;
  margin-bottom: 1.25rem;
}

.unfunded-title {
  font-size: 0.938rem;
  font-weight: 600;
  color: #475569;
}

.unfunded-help {
  margin: 0.25rem 0 0.75rem;
  font-size: 0.813rem;
  color: #64748b;
}

.unfunded-list {
  list-style: none;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 0.375rem 1.5rem;
}

.unfunded-item {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  font-size: 0.813rem;
  padding: 0.25rem 0;
  border-bottom: 1px solid #f1f5f9;
}

.unfunded-name {
  color: #475569;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.unfunded-meta {
  color: #64748b;
  flex-shrink: 0;
}
</style>
