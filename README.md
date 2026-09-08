# ZettaBrain Lite

Local-first RAG + Skills platform, single-user edition. Ingest your own documents, chat with them, and run Skills that generate accurate documents grounded in your corpus. Works with any model — cloud providers or local via Ollama.

## Install

```bash
pipx install "zettabrain-lite[all] @ git+https://github.com/zettabrain/zettabrain-lite.git@skill-quality-and-ingestion"
```

The `[all]` extra is required for price lists: it pulls `pymupdf` for PDF tables.
`openpyxl` (XLSX) is a base dependency and is always installed.

## Run

```bash
zettabrain-lite
# Opens on http://localhost:7860
```

## Requirements

- Python 3.10+
- [Ollama](https://ollama.ai) running locally with `llama3.1` and `nomic-embed-text` pulled
- For cloud providers (OpenAI, Claude, Groq, etc.): API keys configured in Settings

## Features

- **RAG Chat** — hybrid BM25 + semantic search over your documents
- **Skills** — structured document generation (quotes, proposals, reports, compliance)
- **Price List DB** — XLSX/CSV price lists ingested into SQLite for accurate quote generation (no hallucinated prices)
- **Skill Wizard** — AI-assisted skill creation with corpus rule extraction
- **Multi-provider** — Ollama, OpenAI, Claude, Groq, Together, Bedrock, and more
- **OneDrive sync** — connect personal or work/school accounts
- **PDF/Word export** — download generated documents

## Supported file types

| Type | RAG search | Price list DB |
|------|-----------|---------------|
| PDF | ✓ | ✓ (if filename contains price_list, rate_card, catalog, etc.) |
| DOCX / TXT / MD | ✓ | — |
| XLSX / XLS | ✓ | ✓ (always) |
| CSV | ✓ | ✓ (always) |

## Development

```bash
git clone https://github.com/zettabrain/zettabrain-lite.git
cd zettabrain-lite
make install   # pip install -e ".[all]"
make dev       # uvicorn with --reload on port 7860
```
