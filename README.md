# AnySQL

AnySQL is a FastAPI-based platform for analyzing SQL assets with an LLM, indexing SQL and metadata knowledge, and helping users search, generate, revise, and approve SQL.

## Features

- SQL file scanning and statement extraction.
- LLM-assisted SQL summaries, usage guidance, categories, keywords, and business context.
- Metadata-aware analysis using local database metadata snapshots.
- Semantic search with Ollama-compatible embeddings. Team mode stores vectors in PostgreSQL pgvector; ChromaDB remains only for local compatibility.
- SQL generation from product metadata and existing SQL knowledge.
- Human-approved knowledge growth: generated SQL is returned as a draft and enters the knowledge base only after explicit acceptance.
- SQL-only assistant workflow: match existing SQL with LLM-scored confidence, generate when confidence is low, revise from user feedback, and learn accepted generated/revised results.
- Desktop-first SQL workbench with a 1K minimum layout, expanded SQL viewer, syntax coloring, basic SQL formatting, and optional comment stripping for tools with weak comment handling.
- Japanese, Chinese, and English UI language switching, defaulting to Japanese.
- Product management for adding/editing product definitions and always-on product rules used as LLM guardrails.
- Japanese is the retrieval pivot language because product metadata is usually stored in Japanese; non-Japanese user requests are normalized before retrieval.
- Product metadata can be vectorized for RAG and used alongside known SQL examples.
- New products automatically start an initial Metadata sync from the configured database; later syncs are differential, can be triggered manually, and also run daily while the service is running.
- Generated or revised SQL is returned as a draft and is only written to the knowledge base after explicit user acceptance.
- Web UI for search, product status, and analysis progress.
- UTF-8 first handling for Chinese, Japanese, and English SQL assets.

## Team Architecture

The default deployment is now the 50-user team architecture:

- PostgreSQL as the system of record for products, rules, metadata, accepted SQL knowledge, chat history, sync jobs, and audit history.
- pgvector in PostgreSQL as the vector backend.
- Redis/RQ workers for metadata sync, SQL analysis, embedding rebuilds, and scheduled jobs.
- File/object storage only for imported SQL files and exports, not for canonical shared knowledge.
- Docker Compose as the standard deployment group for API, worker, scheduler, PostgreSQL, and Redis.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the implemented architecture and operational notes.

## Repository Policy

The repository intentionally excludes local and generated data:

- `products/`: product SQL, generated descriptions, and metadata snapshots.
- `data/`: local-mode ChromaDB files, logs, and runtime state.
- `deploy-data/`: Docker Compose persistent data for PostgreSQL, Redis, config, imports, exports, and logs.
- `config.yaml`: local configuration and secrets.

Use `config.example.yaml` as the template for local setup.

## Requirements

- Python 3.11 or newer.
- An Ollama-compatible API endpoint exposing:
  - chat model, for example `qwen3:14b`
  - embedding model, for example `bge-m3`
- Optional Oracle client dependency if database metadata must be collected live:
  - `pip install ".[oracle]"`
- Production storage and worker dependencies:
  - `pip install ".[production]"`

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
```

Edit `config.yaml` for your local LLM endpoint, product directories, and database connection.

## Run Locally

```powershell
python -m uvicorn anysql.main:app --host 127.0.0.1 --port 8765
```

Then open:

- Search UI: `http://127.0.0.1:8765/`
- Product page: `http://127.0.0.1:8765/products`
- Analysis dashboard: `http://127.0.0.1:8765/analysis`

The main SQL assistant UI is desktop-first and targets screens of at least 1024px width.
AnySQL does not require user login in the current local deployment model.

## Docker Compose Deployment

Copy `.env.example` to `.env` if you need to override defaults, then start the full service group:

```powershell
docker compose up -d --build
```

The API is exposed on `http://127.0.0.1:8765`. PostgreSQL and Redis are internal Compose services by default.

Persistent host paths:

- `./deploy-data/config:/app/config`
- `./deploy-data/imports:/app/storage/imports`
- `./deploy-data/exports:/app/storage/exports`
- `./deploy-data/logs:/app/storage/logs`
- `./deploy-data/postgres:/var/lib/postgresql/data`
- `./deploy-data/redis:/data`

Containers and images can be deleted and rebuilt safely as long as `deploy-data` is preserved. Deleting `deploy-data` resets the system to an empty database.

### Import Local Product Data

After the Compose group is running, migrate existing local `config.yaml` and `products/` assets into PostgreSQL:

```powershell
docker compose exec api python -m anysql.migrate_local --config /app/config/config.yaml --products-dir /app/products --rebuild-embeddings
```

Generated drafts such as `generated.sql` and `_generated_*.json` are skipped unless they have explicit acceptance records.

## API Overview

- `GET /api/products`
- `GET /api/products/{product_id}`
- `GET /api/products/{product_id}/sqls`
- `POST /api/analysis/{product}/start?force=false`
- `GET /api/analysis/{product}/progress`
- `GET /api/search?q={query}&product={product}&k={top_k}`
- `POST /api/search`
- `POST /api/generate/sql`
- `POST /api/assistant/sql`
- `POST /api/assistant/learn`
- `POST /api/analysis/{product}/metadata-index`
- `POST /api/analysis/{product}/metadata-sync`
- `GET /api/analysis/{product}/metadata-sync`

### SQL Assistant

`POST /api/assistant/sql` is the main user workflow. It is intentionally limited to SQL assistance:

1. Retrieve candidate SQL by semantic search.
2. Ask the LLM to score whether each candidate satisfies the user's requirement.
3. Return an existing SQL when the LLM match score is high enough.
4. Generate a new SQL from product metadata and known SQL knowledge when no candidate is good enough.
5. Revise the current SQL when the user sends corrections or follow-up requirements.
6. Persist accepted generated or revised SQL to PostgreSQL and update pgvector embeddings.

### Generate SQL and Learn

`POST /api/generate/sql` accepts a product and natural-language requirement. The service retrieves similar SQL knowledge, injects product metadata context, and returns a draft SQL. Use `POST /api/assistant/learn` after human acceptance to save it into PostgreSQL and update pgvector.

Example:

```json
{
  "product": "demo",
  "requirement": "Find employees transferred after a target date",
  "top_k": 5
}
```

## Product Layout

Legacy and local-import product files can still use:

```text
products/{product}/sql       # source .sql files
products/{product}/metadata  # table/program metadata cache
products/{product}/desc      # generated SQL analysis JSON
```

These files are import/export assets and are not the team-mode source of truth.

## Validation

```powershell
python -m compileall anysql
docker compose config --quiet
docker compose up -d --build
```

For a running app, basic smoke checks:

```powershell
Invoke-WebRequest http://127.0.0.1:8765/api/products
Invoke-WebRequest "http://127.0.0.1:8765/api/search?q=test&k=3"
```
