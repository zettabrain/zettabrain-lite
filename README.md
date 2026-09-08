# ZettaBrain Lite

Local-first RAG + Skills platform, single-user edition. Ingest your own documents, chat with them, and run Skills that generate accurate documents grounded in your corpus. Works with any model — cloud providers or local via Ollama.

## Install

```bash
pipx install "zettabrain-lite[all] @ git+https://github.com/zettabrain/zettabrain-lite.git@skill-quality-and-ingestion"
```

`[all]` adds the optional providers (OpenAI, Claude), PDF and Word export, and PDF table
reading. The core RAG and Skills features work without it.

## Run

```bash
zettabrain-lite
# Opens on http://localhost:7860
```

## Requirements

- Python 3.10+
- [Ollama](https://ollama.ai) with an embedding model (`nomic-embed-text`) pulled — needed
  to index your documents
- A model to generate with. Any one of:
  - a local Ollama model (`phi4-mini` runs on modest hardware; `llama3.1:8b` is better with a GPU)
  - a cloud provider key in Settings — OpenAI, Claude, Groq, Together, Gemini, Bedrock
  - the built-in free trial, which needs no key and no local model

`install.sh` sets up Ollama and pulls `nomic-embed-text` and `phi4-mini` for you.

## Features

- **RAG Chat** — hybrid BM25 + semantic search over your documents
- **Skills** — reusable instructions that generate a document grounded in your corpus:
  proposals, reports, SOPs, contract summaries, incident reports, engagement letters,
  training material, quotes
- **Skill Wizard** — build a skill by describing what you need; it reads the rules,
  thresholds and prohibitions out of your own documents and writes them into the skill
- **Figures come from your files** — where a skill states numbers, they are looked up from
  your source data and computed in code, not recalled by the model
- **Multi-provider** — Ollama, OpenAI, Claude, Groq, Together, Bedrock, and more
- **OneDrive sync** — connect personal or work/school accounts
- **PDF/Word export** — download generated documents

## Supported file types

Everything you upload is indexed for search and available to every skill.

| Type | Search and skills | Also read as a rate table |
|------|-------------------|---------------------------|
| PDF | ✓ | when the filename names a price list, rate card, catalogue, tariff or fee schedule |
| DOCX / TXT / MD | ✓ | — |
| XLSX / XLS | ✓ | when a product column and a price column are found |
| CSV | ✓ | when a product column and a price column are found |

The second column is an extra, not a requirement. Most skills — proposals, reports,
SOPs, summaries, letters, assessments — need nothing from it.

It exists for skills that put figures in front of a customer. When a spreadsheet turns out
to hold a rate table, ZettaBrain also reads it into a lookup table, so a quote takes its
prices from your file and does the arithmetic in code rather than asking the model to
recall a number. Any rates stated alongside it, such as a VAT line or a bulk discount
threshold, are read the same way. Nothing needs to be reformatted first: a title block
above the header, several sheets, and separate trade or tax columns are all handled.

## Development

```bash
git clone https://github.com/zettabrain/zettabrain-lite.git
cd zettabrain-lite
make install   # pip install -e ".[all]"
make dev       # uvicorn with --reload on port 7860
```
