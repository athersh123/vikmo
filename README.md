Track: aiml

# VIKMO DealerDesk — Automotive Dealer Management System

VIKMO DealerDesk is an enterprise automotive dealer management and intelligent operations platform. Both the **Django Track** and the **AI/ML Track** (including the full-stack visual frontend operations portal) have been completely implemented, verified, and pass all contract smoke tests.

---

## 1. Setup & Run Instructions

### Prerequisites
- Python 3.11+
- PostgreSQL 14+ (local or hosted on Neon/Supabase)

### Step-by-Step Run Contract
```bash
# 1. Create and activate a clean virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Environment configuration
cp .env.example .env
# Edit .env with your DATABASE_URL and GEMINI_API_KEY (or OPENAI_API_KEY / GROQ_API_KEY)

# 4. Database migrations
python manage.py migrate --noinput

# 5. Idempotent database seeding (loads 600 catalogue products and 25 dealers)
python manage.py seed_data

# 6. Launch development server
python manage.py runserver 0.0.0.0:8000 --noreload
```

---

## 2. Running Test Suites

### Unit, Concurrency, and Integration Tests
```bash
# Run all 36 tests across all apps (catalog, orders, supplier, assistant, forecasting)
python manage.py test

# Run individual app test suites
python manage.py test catalog
python manage.py test orders
python manage.py test supplier
python manage.py test assistant
python manage.py test forecasting
```

### Smoke Test Validation
```bash
# AI/ML Track verification (Core, Assistant, Forecasting)
python smoke_test.py --base-url http://localhost:8000 --track aiml

# Django Track verification (Core, Order Engine, Supplier Sync)
python smoke_test.py --base-url http://localhost:8000 --track django

# Full check across all endpoints
python smoke_test.py --base-url http://localhost:8000 --all
```

---

## 3. LLM Provider & Model Configuration (Phase 4)

- **Primary Provider**: **Google Gemini**
- **Model**: **`gemini-2.5-flash`** (default, configurable via `GEMINI_MODEL`)
- **Environment Variable**: `GEMINI_API_KEY`
- **Temperature**: `0.1` (deterministic function calling and grading consistency)
- **Timeouts & Safety Ceilings**: Client timeout of 25 seconds per call; maximum of 5 tool-loop iterations per user turn.
- **Multi-Provider Fallback**: Transparently adapts to `OPENAI_API_KEY` (`gpt-4o-mini`) or `GROQ_API_KEY` (`llama-3.3-70b-versatile`) if configured in the grader environment.
- **Offline Fallback**: Offline rule-based handler enables mock testing and offline evaluation without network access.
- **Function Tools**:
  - `search_parts(query, vehicle?, category?, limit?)`: Hybrid BM25 + trigram RRF retrieval over 600 products, reading live pricing and stock from DB.
  - `check_stock(sku)`: Fetches live `Inventory.quantity` and current `Product.price`.
  - `create_draft_order(items:[{sku,quantity}])`: Calls `OrderService.create_draft_order()` with server-injected `dealer_id`. Refuses confirmation (tells dealer to confirm in portal/API).

---

## 4. Optional & Bonus Parts Attempted (Phase 6)

1. **Both Tracks Fully Delivered**:
   - **Full Django Track**: Order state machine (`DRAFT -> CONFIRMED -> DISPATCHED -> DELIVERED`, cancel restores stock), deadlock-free consistent inventory row locking, per-line 18% GST half-up rounding, atomic gap-safe sequence generation (`ORD-YYYYMMDD-NNNN`), PostgreSQL advisory-lock idempotency, and supplier sync engine with 5 retries, 429 `Retry-After`, and latest-timestamp deduplication.
   - **Full AI/ML Track**: Real tool-calling assistant (`POST /api/assistant/chat/`), prompt injection defense, multi-horizon demand forecasting (`ml/forecast.csv`), and live reorder suggestions (`GET /api/reorder-suggestions/`).
2. **Interactive Operations Portal & Frontend**:
   - Served out-of-the-box by Django at `http://localhost:8000/` (`templates/index.html`).
   - Standalone modular React 18 / Vite application in `frontend/` (`frontend/src/App.jsx`).
   - Cross-Origin Resource Sharing (`django-cors-headers`) enabled (`CORS_ALLOW_ALL_ORIGINS = True`).
   - Includes real-time debounced catalogue search, stock badges, cart drawer, draft creation, confirmation with structured `409 INSUFFICIENT_STOCK` error feedback, AI chat drawer with tool-call chips, reorder math table, and 4-week forecast visualizer.

---

## 5. Walkthrough Video Link

- **Video Link**: `https://www.youtube.com/watch?v=dQw4w9WgXcQ` *(Placeholder — replace with your recorded 5-minute Loom / YouTube walkthrough demo)*

---

## 6. Assumptions Made

1. **Database Platform**: PostgreSQL is strictly enforced per contract specifications. `DATABASE_URL` is parsed via `dj-database-url`.
2. **Stock Decoupling Invariant**: Warehouse physical stock resides exclusively in `Inventory.quantity`. Catalog synchronization updates product specifications and prices, but never mutates warehouse inventory.
3. **Draft Pricing vs Confirmed Pricing**: Draft orders reflect live catalog price changes dynamically; upon transition to `CONFIRMED`, line unit prices are permanently locked against future catalog modifications.
4. **GST Rounding Standard**: Indian GST (18%) is calculated and rounded per line item using `ROUND_HALF_UP` to the nearest paisa (`Decimal("0.01")`). Order subtotal, tax, and grand total are sums of the line amounts.
5. **Multi-Horizon Independence**: Future demand forecasts for horizons $h \in \{1, 2, 3, 4\}$ are predicted using direct models to prevent recursive feedback of forecast errors.
6. **Diwali Festival Adjustment**: The festive demand surge is indexed by signed weeks relative to the actual festival date in `data/festival_calendar.csv` rather than fixed Gregorian calendar weeks.
