# DeltaDeFi AMM Market Maker — Parameter Guide

## How the AMM Works

The strategy simulates a constant-product AMM (`k = base × quote`) using limit orders on the DeltaDeFi order book. It maintains a **virtual pool** of reserves that tracks fills and determines pricing via the k-curve.

### Pricing Formula

```
blended_mid = (1 - pool_price_weight) × anchor_mid + pool_price_weight × pool_price
```

Where:
- `pool_price = quote / base` — the raw k-curve price (AMM component)
- `anchor_mid = anchor_price × (1 + shift)` — inventory-shifted book mid (PMM component)
- `shift = (1/inventory_ratio - 1) / amplification`

Orders are placed at:
- `ask = blended_mid × (1 + base_spread_bps / 10000)`
- `bid = blended_mid × (1 - base_spread_bps / 10000)`

### Fill-Induced Price Shift

When a fill of size `dB` occurs on a pool with base reserves `B`:

```
blended_shift = (dB/B) × [(1 - pool_price_weight) / amplification + 2 × pool_price_weight]
```

For stability (no ping-pong), this shift must be less than the spread:

```
fill_shift < base_spread_bps / 10000
```

---

## Core Parameters

### `exchange`
- **Default:** `"deltadefi"`
- **What:** The exchange connector to use.

### `trading_pair`
- **Default:** `"ADA-USDM"`
- **What:** The market to trade on. Must match a key in the exchange's trading rules.
- **Presets:** `ADA-USDM`, `IAG-USDM`, `NIGHT-USDM` have built-in initial prices.

### `initial_price`
- **Default:** From pair presets, or `0.01` if not found.
- **What:** Starting price for the virtual pool when no book mid is available. Used only on first initialization.

---

## Pool Shape Parameters

These control the virtual AMM curve — how the price responds to fills.

### `max_pool_depth`
- **Default:** `50,000` (USDM)
- **What:** Maximum size of the virtual pool's quote reserves. Determines the k-curve shape.
- **Effect on pricing:**
  - Larger pool = flatter curve = less price impact per fill
  - Smaller pool = steeper curve = more price impact per fill
- **Key insight:** This is VIRTUAL — independent of real account balance. You can set 50,000 with only 2,000 USDM in the account. The balance_gate separately limits actual order sizes to real balance.
- **Math:** `pool.initial_base = max_pool_depth / price`, so at price 0.27:
  - 10,000 USDM → 37,037 ADA virtual base
  - 50,000 USDM → 185,185 ADA virtual base
- **Tuning:** Increase to reduce ping-pong (fill shift). The fill shift formula:
  ```
  fill_shift ≈ pool_price_weight × 2 × order_size / pool_base
  ```
  With depth 50,000 and order 42 ADA: shift = 0.7 × 2 × 42/185,185 = 0.03%

### `min_pool_depth`
- **Default:** `None` (no minimum)
- **What:** Minimum virtual pool depth. Prevents the pool from being too shallow on low-balance accounts.

### `amplification`
- **Default:** `20`
- **What:** Dampens the inventory shift in the PMM (anchor) component of the blended price.
- **Effect:** Only affects the `(1 - pool_price_weight)` portion of the blend.
  - Higher amplification = flatter PMM curve = anchor_mid barely moves with inventory
  - Lower amplification = steeper PMM curve = anchor_mid shifts more with inventory
- **At pool_price_weight=0.7:** The PMM component is only 30% of the blend, and amplification=20 makes it nearly flat. The PMM contribution to fill shift is `(0.3/20) = 0.015` — negligible compared to the AMM component `(2 × 0.7) = 1.4`.
- **When it matters:** At low pool_price_weight (PMM-dominant mode), amplification controls how much the anchor shifts with inventory.

### `floor_ratio`
- **Default:** `0.30`
- **What:** Fraction of initial reserves held back as a floor. The strategy only uses reserves above this floor for order sizing.
- **Effect:** `available_base = pool.base - initial_base × 0.30`
- **Purpose:** Prevents the pool from being fully drained, which would make the k-curve extreme.

---

## Spread & Order Parameters

### `base_spread_bps`
- **Default:** `20` (0.20%)
- **What:** Half-spread applied to each side of the blended mid price, in basis points.
- **Critical constraint:** Must be larger than the fill-induced price shift to avoid ping-pong:
  ```
  base_spread_bps / 10000 > pool_price_weight × 2 × order_size / pool_base
  ```
- **Tapster relationship:** Set tighter than Tapster's spread so the AMM captures flow inside Tapster's quotes. Tapster needs wider spreads due to hedging costs (CEX fees + slippage).

### `order_amount_pct`
- **Default:** `0.015` (1.5% of available reserves)
- **What:** Fraction of available virtual reserves to offer per level per side.
- **Actual size:** `order = order_amount_pct × available_reserves × level_weight`, then capped by balance_gate to real balance.
- **Effect on stability:** Smaller orders = less k-curve shift per fill = more stable pricing.
- **Trade-off:** Too small → many fills needed for the k-curve to shift meaningfully. Too large → ping-pong.

### `num_levels`
- **Default:** `1`
- **What:** Number of order levels per side (bid/ask).
- **Multi-level:** Each level is placed progressively further from mid, with spread scaled by `spread_multiplier` and size scaled by `size_decay`.

### `spread_multiplier`
- **Default:** `1.5`
- **What:** Each subsequent level's spread is multiplied by this factor.
- **Example (3 levels, 20 bps base):** Level 1: 20 bps, Level 2: 30 bps, Level 3: 45 bps.

### `size_decay`
- **Default:** `0.85`
- **What:** Each subsequent level gets this fraction of the previous level's size.
- **Example (3 levels):** Weights 1.0, 0.85, 0.72 (normalized to sum to 1).

### `order_refresh_time`
- **Default:** `5` seconds
- **What:** In timer mode (`refresh_on_fill_only=False`), orders are cancelled and replaced every N seconds.

### `refresh_on_fill_only`
- **Default:** `True`
- **What:** When true, orders stay on the book until filled. After a fill, all orders are cancelled and requoted from the updated pool state. When false, orders refresh on a timer.
- **Recommended:** `True` for AMM mode — the pool state only changes on fills, so requoting on timer is unnecessary.

---

## Hybrid Pricing Parameters

### `pool_price_weight`
- **Default:** `0.70`
- **Range:** `[0, 1]`
- **What:** Controls the blend between AMM pricing (k-curve) and PMM pricing (anchor).
  - `0.0` = Pure PMM — quotes around book mid, ignores k-curve. Identical to pure_market_making.
  - `1.0` = Pure AMM — quotes from k-curve only, no anchor. Risk: gets drained on trends.
  - `0.7` = 70% AMM + 30% PMM — strong k-curve opinion with anchor as safety leash.
- **Tapster context:** Since Tapster already does PMM (price discovery + hedging), the AMM should have a high weight to differentiate. The AMM provides k-curve liquidity; Tapster provides hedged backstop liquidity.

### `anchor_ema_alpha`
- **Default:** `0.05`
- **Range:** `(0, 1]`
- **What:** Exponential moving average smoothing factor for anchor price tracking.
  ```
  new_anchor = alpha × book_mid + (1 - alpha) × old_anchor
  ```
  - `0.05` = slow tracking — anchor adjusts 5% toward book mid each tick
  - `1.0` = instant snap — anchor = book mid (old behavior)
  - `0.01` = very slow — anchor drifts gradually
- **Purpose:** Prevents anchor from jumping on every tick (reduces jitter). With high pool_price_weight, this has minor effect since anchor is only 30% of the blend.

---

## Safety Parameters

### `max_cumulative_loss`
- **Default:** `500` (USDM)
- **What:** Maximum allowed mark-to-market loss. If P&L drops below `-max_cumulative_loss`, the strategy cancels all orders and stops.
- **P&L calculation:** `P&L = base_flow × book_mid + quote_flow` (mark-to-market).

### `min_base_balance`
- **Default:** `1,000` (ADA)
- **What:** If real base balance drops below this, all SELL orders are removed. Prevents overselling.

### `min_quote_balance`
- **Default:** `500` (USDM)
- **What:** If real quote balance drops below this, all BUY orders are removed. Prevents overbuying.

### `balance_buffer_pct`
- **Default:** `0.90` (90%)
- **What:** Orders are scaled to use at most this fraction of real balance. Leaves 10% as buffer for fees and slippage.

### `min_both_sides_pct`
- **Default:** `0.40`
- **What:** If BOTH virtual base and quote reserves drop below 40% of initial, the strategy pauses (cancels all orders). Indicates the pool is severely depleted on both sides.

### `rebalance_threshold`
- **Default:** `0.02` (2%)
- **What:** If the blended mid diverges from book mid by more than this percentage, a market rebalance order is placed to realign the pool.
- **With high pool_price_weight:** This acts as a circuit breaker — if the k-price drifts too far from market, force-realign rather than let the AMM be adversely selected.

### `rebalance_cooldown`
- **Default:** `60` seconds
- **What:** Minimum time between rebalance events. Prevents rapid-fire rebalancing.

---

## Enhancement Flags

### Fill Velocity Detector
- **`enable_fill_velocity_detector`** (`False`): Enable/disable.
- **`fill_velocity_window_sec`** (`10`): Rolling window for counting fills.
- **`fill_velocity_max_same_side`** (`3`): If N fills on the same side happen within the window, pause. Detects one-sided adverse flow.

### Asymmetric Spread
- **`enable_asymmetric_spread`** (`False`): Enable/disable.
- **`skew_sensitivity`** (`0.5`): How much inventory skew widens/tightens spreads.
- **`min_spread_bps`** (`20`): Floor spread — never go below this even with skew.
- **Effect:** When the pool is long base (excess ADA), widen the bid spread (discourage more buying) and tighten the ask spread (encourage selling). Helps rebalance inventory.

### Order Randomization
- **`enable_order_randomization`** (`False`): Enable/disable.
- **`randomization_pct`** (`0.15`): Jitter range for order sizes (±15%).
- **Purpose:** Anti-detection — makes order patterns less predictable.

---

## Recommended Configurations

### AMM Inside Tapster (Default)
```yaml
pool_price_weight: 0.70
base_spread_bps: 20
max_pool_depth: 50000
order_amount_pct: 0.015
amplification: 20
```
AMM quotes tight inside Tapster. Tapster provides backstop + hedging. K-curve shifts slowly per fill, accumulates over many fills for mean-reversion profit.

### Conservative Start
```yaml
pool_price_weight: 0.30
base_spread_bps: 40
max_pool_depth: 50000
order_amount_pct: 0.02
```
Lower AMM weight for less k-curve exposure. Good for testing or volatile markets.

### Pure PMM (Legacy)
```yaml
pool_price_weight: 0.0
base_spread_bps: 40
max_pool_depth: 10000
order_amount_pct: 0.05
```
Exact old behavior. No k-curve influence. Functionally identical to pure_market_making.

---

## Stability Check

Before deploying, verify the no-ping-pong condition:

```
fill_shift = (order_size / pool_base) × [(1-w)/A + 2w]
spread = base_spread_bps / 10000

REQUIRED: fill_shift < spread / 2
```

With defaults (w=0.7, A=20, depth=50000, price=0.27, order≈42 ADA):
```
pool_base = 50000 / 0.27 = 185,185
fill_shift = (42/185185) × [0.3/20 + 1.4] = 0.000227 × 1.415 = 0.032%
spread = 20 bps = 0.20%
0.032% < 0.10% ✓ STABLE
```
