# AnySQL

AnySQL is a FastAPI-based platform for analyzing SQL assets with an LLM, indexing the results in ChromaDB, and searching SQL by natural language.

## Features

- SQL file scanning and statement extraction.
- LLM-assisted SQL summaries, usage guidance, categories, keywords, and business context.
- Metadata-aware analysis using local database metadata snapshots.
- ChromaDB semantic search with Ollama-compatible embeddings.
- Web UI for search, product status, and analysis progress.
- UTF-8 first handling for Chinese, Japanese, and English SQL assets.

## Repository Policy

The repository intentionally excludes local and generated data:

- `products/`: product SQL, generated descriptions, and metadata snapshots.
- `data/`: ChromaDB files, logs, and runtime state.
- `config.yaml`: local configuration and secrets.

Use `config.example.yaml` as the template for local setup.

## Requirements

- Python 3.11 or newer.
- An Ollama-compatible API endpoint exposing:
  - chat model, for example `qwen3:14b`
  - embedding model, for example `bge-m3`
- Optional Oracle client dependency if database metadata must be collected live:
  - `pip install ".[oracle]"`

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
```

Edit `config.yaml` for your local LLM endpoint, product directories, and database connection.

## Run

```powershell
python -m uvicorn anysql.main:app --host 127.0.0.1 --port 8768
```

Then open:

- Search UI: `http://127.0.0.1:8768/`
- Product page: `http://127.0.0.1:8768/products`
- Analysis dashboard: `http://127.0.0.1:8768/analysis`

## API Overview

- `GET /api/products`
- `GET /api/products/{product_id}`
- `GET /api/products/{product_id}/sqls`
- `POST /api/analysis/{product}/start?force=false`
- `GET /api/analysis/{product}/progress`
- `GET /api/search?q={query}&product={product}&k={top_k}`
- `POST /api/search`

## Product Layout

For each configured product:

```text
products/{product}/sql       # source .sql files
products/{product}/metadata  # table/program metadata cache
products/{product}/desc      # generated SQL analysis JSON
```

These files are local runtime/product assets and are not committed to Git.

## Validation

```powershell
python -m compileall anysql
```

For a running app, basic smoke checks:

```powershell
Invoke-WebRequest http://127.0.0.1:8768/api/products
Invoke-WebRequest "http://127.0.0.1:8768/api/search?q=test&k=3"
```
