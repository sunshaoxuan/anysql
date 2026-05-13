# Changelog

All notable changes to AnySQL are documented in this file.

## [0.6.2] - 2026-05-14

### Changed

- Invalid assistant runs no longer display blocked SQL in the main SQL pane.
- Knowledge Gap progress now shows user-facing review status instead of raw validator noise in the chat result.
- Self-improvement evidence mining now downranks payroll/input/XML/provisional tables and requires strong dependent/support field markers before proposing table-field candidates.
- Rerun Knowledge Gap analysis clears stale proposed candidates before producing a new review pack.

## [0.6.1] - 2026-05-13

### Added

- Added observable Knowledge Gap progress fields so background self-improvement tasks expose their current stage, percentage, message, and detail payload.
- Added live Assistant-side polling for `knowledge_gap_id` so invalid runs show the ongoing backend improvement status in the chat result.
- Added auto-refreshing Product UI progress bars for queued/running Knowledge Gap analysis, including candidate table and term previews.

## [0.6.0] - 2026-05-13

### Added

- Added the Knowledge Gap workflow for invalid/unsafe assistant runs, including PostgreSQL persistence for `knowledge_gaps` and `knowledge_candidates`.
- Added background `knowledge_gap_analysis` RQ task to mine business terms, explore Metadata evidence, and propose reviewable intent/table/field/predicate candidates.
- Added Knowledge Gap review APIs for listing, viewing, approving, rejecting, and rerunning gap analysis.
- Added Product UI controls for reviewing knowledge gaps and approving or rejecting proposed self-improvement candidates.

### Changed

- Assistant responses now return `source=needs_knowledge_review` and `knowledge_gap_id` when a draft is invalid and requires self-improvement review.
- Unknown intents now require safe business evidence before being treated as verified; dangerous table roles are blocked from unknown-intent context.
- Approved gap candidates are indexed into RAG as medium-weight evidence, while unapproved candidates stay out of retrieval.
- Alembic startup now commits extension setup before migrations so version advancement is reliable.

### Fixed

- SQL validator now treats aggregate/common SQL functions such as `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `NVL`, and `DECODE` as functions instead of unknown columns.
- Dependent-child/family-support requests with only employee-number evidence are invalidated instead of being shown as usable SQL.

## [0.5.0] - 2026-05-13

### Added

- Added a Harness Agent foundation with intent planning, evidence bundles, validation results, run IDs, and LLM call accounting on assistant responses.
- Added multi-dimensional RAG storage: `rag_nodes`, `rag_embeddings`, `rag_terms`, `join_edges`, `agent_runs`, `agent_steps`, and `feedback_events`.
- Added product APIs for RAG rebuild/stats and join edge inspection/editing.
- Added agent run audit API at `GET /api/agent-runs/{run_id}`.
- Added immediate positive-feedback RAG nodes and embeddings when users accept generated SQL.
- Added Product UI controls for RAG rebuild/stats and join edge inspection.
- Added LLM-assisted table profile review for ambiguous Metadata tables, constrained to fixed domain/role enums with deterministic fallback.
- Added a single-table LLM profile classification API so suspicious table roles can be reviewed without blocking full product rebuilds.

### Changed

- Switched the default embedding model from `bge-m3` to `qwen3-embedding:8b` and migrated pgvector columns to 4096 dimensions.
- Changed Metadata RAG field nodes from per-column vectors to per-table column chunks so full rebuilds fit the ccnode embedding capacity.
- Database-mode assistant requests now use an evidence-first Harness Agent path before falling back to the legacy flow.
- Metadata sync and embedding rebuild jobs now prepare multi-dimensional RAG nodes in addition to legacy pgvector embeddings.
- Metadata sync and manual profile rebuild now use deterministic profiling plus a bounded qwen3 review pass for high-priority conflicting table evidence.
- Assistant UI now displays intent/evidence/validation summaries and hides acceptance when validation fails.
- Merged the old Analysis dashboard navigation into the Product / Knowledge management entry; SQL analysis can now be started from the product page.
- Updated operation and design docs to reflect Harness Agent, RAG, Docker port 8765, and test-data cleanup rules.

### Fixed

- Added deterministic validation gates for evidence-table drift, unknown evidence fields, missing name conditions, and unsafe generated parameter names.
- Tightened automatic table profile inference so part-time employee basic master tables such as `DJND3001` are classified as `employee/master` instead of drifting to `transfer/master`.

## [0.4.0] - 2026-05-13

### Added

- Added database-backed table role catalog with deterministic domain/role profiling for Metadata tables.
- Added product APIs to list, manually update, and rebuild table profiles.
- Added Product UI controls for searching and editing table domain/role profiles.
- Added intent policies for employee basic information, transfer records, and transfer check logs.

### Changed

- Assistant and generation Metadata retrieval now applies table role constraints before sending candidates to the LLM.
- Metadata sync and local migration now rebuild automatic table profiles while preserving manual overrides.
- Transfer record queries now prefer `DKIDO_R` / `DKIDO` and block check logs such as `XCIDOCHKLOG`.

### Fixed

- Prevented ordinary business queries from using log/work/IF/backup tables unless the intent explicitly allows them.
- Removed unrequested bare parameter filters for all-record requests such as `WHERE CSHAINNO LIKE :1`.

## [0.3.3] - 2026-05-13

### Added

- Added a SQL alias toggle in the assistant UI, defaulting to off.
- Added `use_aliases` to assistant requests so generation can explicitly allow or suppress column aliases.

### Changed

- Default SQL cleanup now removes generated column aliases, including `AS alias` aliases and non-ASCII implicit aliases.
- Employee basic-information metadata retrieval now applies a non-LLM intent reranker so personal master tables rank above payroll, work, history, IF, and backup tables.

### Fixed

- Fixed quoted name-parameter patterns such as `LIKE '':name_pattern'%` by replacing them with the user's actual surname literal.
- Removed trailing employee-number filters from surname/name searches.

## [0.3.2] - 2026-05-12

### Fixed

- Removed the hardcoded surname example from runtime LLM prompts to avoid generated SQL copying sample values.
- Added SQL cleanup coverage for explicit surname requests so generated name filters preserve the user's actual value, including `丰田` / `豊田`.
- Removed generated employee-number OR conditions from surname/name searches when the user only asked for a name filter.
- Normalized mixed literal-plus-parameter LIKE clauses such as `LIKE '丰田%' || :name || '%'` into directly executable SQL.

## [0.3.1] - 2026-05-12

### Changed

- Reduced assistant generation latency by replacing LLM-based query normalization with local Japanese metadata query expansion.
- Skipped LLM match validation when vector retrieval has no sufficiently strong SQL candidate, so low-match generation uses one LLM call.
- Tightened generation prompts to require Japanese SQL comments, executable Oracle SQL literals, and ASCII-only aliases when aliases are unavoidable.

### Fixed

- Fixed SQL viewer highlighting so single quotes are displayed as real quotes instead of HTML entities.
- Preserved SQL comment line breaks when formatting and toggling comments in the SQL viewer.
- Removed non-ASCII generated column aliases from SQL drafts.
- Replaced broken LLM placeholder literals such as `LIKE '[:1]%'`, `LIKE '%%'`, and generated name parameters with the explicit value provided by the user.
- Removed unrequested generated company/date/employee-number filters from surname/name searches.

## [0.3.0] - 2026-05-12

### Added

- Added PostgreSQL/pgvector as the team-mode system of record and vector backend.
- Added SQLAlchemy models and Alembic migration for products, database connections, SQL records, analyses, metadata, drafts, acceptances, jobs, and embeddings.
- Added repository boundaries for product, SQL knowledge, metadata, jobs, and vectors.
- Added Redis/RQ worker and scheduler processes for metadata sync, SQL analysis, and embedding rebuild jobs.
- Added Docker Compose deployment group with API, worker, scheduler, PostgreSQL, and Redis.
- Added persistent `deploy-data` volume layout for config, imports, exports, logs, PostgreSQL, and Redis.
- Added local migration CLI for importing `config.yaml` and `products/` into PostgreSQL and rebuilding embeddings.

### Changed

- Switched the default runtime architecture from local files/Chroma to PostgreSQL + pgvector + Redis/RQ.
- Kept existing API paths while moving product, search, generation, assistant, and job behavior behind database-backed repositories.
- Generated and revised SQL remain drafts until explicitly accepted through `/api/assistant/learn`.
- Updated documentation to describe the implemented team architecture and Docker recovery behavior.
- Bumped package version from `0.2.0` to `0.3.0`.

### Fixed

- Prevented database-mode SQL generation and learning from writing accepted knowledge back into local product files.
- Prevented the scheduler from enqueueing a full daily sync immediately on every container restart.

## [0.2.0] - 2026-05-12

### Added

- Added a 50-user service architecture document covering PostgreSQL, pgvector, Redis workers, durable jobs, and migration phases.
- Added explicit `storage`, `queue`, and expanded `vector_db` configuration blocks.
- Added production dependency group for PostgreSQL, SQLAlchemy, Alembic, Redis/RQ, and pgvector.
- Added Chroma HTTP vector backend configuration support for service-mode Chroma deployments.

### Changed

- Documented local file/Chroma persistence as development mode rather than the target shared deployment architecture.
- Updated the example server port to `8765`.

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
