# DealerDesk starter

A minimal Django project that already follows the run contract. It has **no models and no business logic**: designing those is the assignment.

What's here:

- `dealerdesk/settings.py` reads `DATABASE_URL`, `SUPPLIER_API_URL`, `SUPPLIER_API_KEY` and the LLM keys (Gemini, OpenAI, Anthropic, Groq), refuses anything but PostgreSQL, and turns off authentication for the API.
- `GET /api/health/` returns `{"status": "ok"}`.
- `python manage.py seed_data` loads 600 catalogue products and 25 dealers idempotently.
- `requirements.txt` with Django, DRF, psycopg, and requests.

## LLM Provider & Dealer Assistant (Phase 4)
* **Primary Provider**: **Google Gemini**
* **Model**: **`gemini-2.5-flash`** (configurable via `GEMINI_MODEL`, default: `gemini-2.5-flash`)
* **Environment Variable**: `GEMINI_API_KEY`
* **Temperature**: `0.1` (deterministic low-temperature tool calling)
* **Fallback Compatibility**: Automatically detects and uses `OPENAI_API_KEY` (`gpt-4o-mini`) or `GROQ_API_KEY` (`llama-3.3-70b-versatile`) if configured by the grader.
* **Assistant Endpoint**: `POST /api/assistant/chat/` with real tool calling (`search_parts`, `check_stock`, `create_draft_order`), hybrid BM25 + trigram RRF retrieval, grounding checks, and prompt injection defenses.

## Set up

1. Copy everything in this folder into the root of your repo, next to `data/`.
2. Get a PostgreSQL database:
   - **Hosted, free:** create a project on neon.tech or supabase.com and copy its connection string, or
   - **Local:** install PostgreSQL and run `createdb dealerdesk`.
3. Copy `.env.example` to `.env` and fill in `DATABASE_URL` (and your LLM key if you build the assistant).
4. Install and run:

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_data
python manage.py runserver 0.0.0.0:8000
```

5. Check it: `curl http://localhost:8000/api/health/` → `{"status": "ok"}`

## Before you submit

Reset to a fresh database, seed twice, and run the smoke test from the bundle:

```bash
python smoke_test.py --base-url http://localhost:8000 --track django   # or --track aiml
```

Add new packages to `requirements.txt` as you go. The grader installs it on Linux with Python 3.11+.

Docker is optional. If you add a `Dockerfile` or `docker-compose.yml` for yourself, keep the plain commands above working too: the grader uses those, not Docker.
