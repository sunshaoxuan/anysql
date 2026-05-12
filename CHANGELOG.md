# Changelog

All notable changes to AnySQL are documented in this file.

## [0.1.10] - 2026-05-12

### Added

- New products now automatically start an initial Metadata sync from the configured database after save.
- Added `POST /api/analysis/{product}/metadata-sync` for manual background database Metadata sync plus vector refresh.
- Added `GET /api/analysis/{product}/metadata-sync` for Metadata sync status.
- Added a Product management sync action for manually refreshing Metadata.
- Added a daily in-process Metadata differential sync task.

### Changed

- Oracle Metadata collection now writes table and column comment snapshots into product metadata files.
- Metadata vector indexing is now differential and only embeds changed Metadata documents while deleting stale entries.

### Fixed

- Fixed the Product management new-product focus target.

## [0.1.9] - 2026-05-12

### Added

- Added Japanese query normalization before SQL and metadata retrieval.
- Added product metadata vector indexing and metadata RAG search.
- Added `POST /api/analysis/{product}/metadata-index` to rebuild metadata vectors.
- Added `POST /api/assistant/learn` so users explicitly accept generated/revised SQL before it is persisted.

### Changed

- Assistant-generated and revised SQL now returns as a draft and no longer auto-pollutes the knowledge base.

## [0.1.8] - 2026-05-12

### Changed

- Product database passwords are treated as saved reference data and are returned to the editor.
- Product password editing now supports masked display, reveal/hide, and copy controls.

## [0.1.7] - 2026-05-12

### Changed

- Added immutable product `physical_id` and renamed the editable UI identifier to Code.
- Product editing now uses `physical_id` as identity, so changing Code cannot create stale undeletable products.
- Localized remaining Product management field labels.

## [0.1.6] - 2026-05-12

### Added

- Added product deletion support in the Product management API and UI.

### Fixed

- Fixed product ID editing so changing an existing product ID migrates the configuration key instead of leaving an undeletable stale product behind.
- Preserved existing database passwords when editing a product without entering a replacement password.

## [0.1.5] - 2026-05-12

### Added

- Added basic i18n support for Japanese, Chinese, and English, defaulting to Japanese.
- Added editable product rules that are always injected into SQL match, generation, and revision prompts.
- Added product create/edit API and UI for product directories, database settings, and rules.

### Changed

- Moved product selection to the chat input area so users can switch target products while working.
- Removed login/user display from the UI.
- Simplified navigation and workbench actions into icon-first controls with hover titles.

## [0.1.4] - 2026-05-12

### Changed

- Reworked the assistant UI as a desktop-first workbench with a 1024px minimum width.
- Allocated the workbench with a golden-ratio split: roughly 38.2% chat and 61.8% SQL workspace.
- Expanded the SQL viewer and added basic formatting plus keyword, string, parameter, number, and symbol coloring.
- Added a comment toggle so displayed/copied SQL can include comments or strip all SQL comments.

## [0.1.3] - 2026-05-12

### Added

- Added SQL-only assistant API at `POST /api/assistant/sql`.
- Added LLM-scored candidate matching before deciding whether to reuse existing SQL or generate a new one.
- Added revision flow for user corrections against the current SQL, with automatic persistence and indexing.
- Reworked the home page into a chat-style SQL assistant workspace with product selection, match scoring, current SQL context, and reset control.

## [0.1.2] - 2026-05-12

### Added

- Added SQL generation request/response models.
- Added `POST /api/generate/sql` for generating SQL from product metadata and existing SQL knowledge.
- Added automatic knowledge growth for generated SQL: append to local product SQL store, analyze, persist description JSON, and update the vector index.
- Documented the generate-and-learn workflow in `README.md`.

## [0.1.1] - 2026-05-12

### Added

- Added repository-ready project metadata via `pyproject.toml`.
- Added `README.md` with setup, runtime, API, and repository data policy.
- Added `config.example.yaml` for safe local configuration bootstrapping.
- Added `.gitignore` to exclude local secrets, runtime data, product assets, caches, and generated files.

### Changed

- Bumped package version from `0.1.0` to `0.1.1`.

### Fixed

- Mounted product API routes in the FastAPI app.
- Passed all configured LLM parameters into `LLMClient`.
- Closed the LLM HTTP client during application shutdown.
- Made static/template paths independent of the process working directory.
- Initialized file logging from configuration.
- Added structured Agent execution for Pydantic response models.
- Fixed analysis pipeline calls to missing metadata helper methods.
- Persisted failed analysis records with error details.
- Added `metadata_snapshot` to `SQLRecord`.
- Preserved comments, keywords, and business context in vector search metadata.
- Escaped rendered search result content in the frontend.

## [0.1.0] - 2026-05-11

### Added

- Initial SQL parsing, LLM analysis, ChromaDB search, FastAPI APIs, and web UI prototype.
