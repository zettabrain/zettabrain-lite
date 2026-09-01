# ZettaBrain Lite

Local-first RAG plus Skills platform, single-user edition. Users ingest their own documents, chat with them, and run Skills that generate documents grounded in that corpus. Works with any model: cloud providers or local via Ollama.

## Architecture

- `zettabrain_lite/server.py`: FastAPI app. All HTTP routes live here.
- `zettabrain_lite/static/index.html`: the entire frontend. HTML, CSS, and JS in one file, no build step, no framework. Keep it that way.
- `zettabrain_lite/generation/`: skill parsing and the generation engine.
- `zettabrain_lite/llm/`: provider factory. Cloud providers and Ollama behind one interface.
- `zettabrain_lite/retrieval.py`: hybrid and advanced retrieval.
- `skills/`: user SKILL.md files. Built-in skills ship separately.

## Ground rules

**The frontend is one file on purpose.** No bundler, no npm, no framework. New UI goes into `index.html` as plain JS. If a change would require a build step, propose it before writing it.

**Local-first is a product commitment, not a default.** Every feature must work with a small local Ollama model and no internet. Cloud models are an upgrade, never a requirement. Before adding anything that calls a model, ask what happens on a 7B local model, and make sure there is a sensible path when the call fails or times out.

**Never assume a model is fast.** Any new LLM call needs a visible progress state in the UI and a fallback when it fails. Multi-call flows must degrade to something useful rather than erroring out.

**Users are not developers.** The people using this are consultants, clinicians, lawyers, and marketers. Error messages say what to do next, not what went wrong internally. No stack traces, no jargon, no "invalid schema."

**Industry-agnostic by construction.** The product must work for any industry without an industry taxonomy. Domain knowledge comes from the user's corpus, never from a hardcoded category list. If a feature needs an industry dropdown to work, the design is wrong.

## Conventions

- Python 3.10+, ruff with line length 120, rules E, F, I, W. Run `ruff check .` before finishing.
- `known-first-party = ["zettabrain_lite"]` for import sorting.
- Type hints on new function signatures. Existing untyped code does not need retrofitting as part of an unrelated change.
- No new runtime dependencies without asking. Reuse what is already in `pyproject.toml`.
- Server helpers are prefixed with `_` and defined in `server.py`. Reuse `_resolve_llm_for_chat`, `_build_corpus_retriever`, `_get_chunk_count`, and `_get_sources` rather than reimplementing them.
- Frontend functions are global and called from inline `onclick` handlers. Match that pattern.

## Skills

A SKILL.md is YAML frontmatter plus markdown instructions. Required frontmatter: `name`, `version`, `description`.

The quality bar for a skill: it must carry information the model would not otherwise have. Thresholds, named approvers, prohibitions, and abstention rules are the point. A skill that only lists section headings performs no better than no skill, so anything that generates or validates skills should enforce this.

Skills will eventually do more than generate documents. Do not hardcode the assumption that a skill produces a document. When touching skill schema or parsing, keep room for other skill types.

## Running

```bash
make install     # pip install -e ".[all]"
make dev         # uvicorn on port 7860, reload enabled
ruff check .
```

Check the `tests/` directory for the existing test setup before adding tests, and follow whatever pattern is there.

## Verification expectations

Claiming something works is not the same as showing it. For any change:

- Python: confirm it imports and, where practical, exercise the new path with a stub rather than only reading the code.
- Frontend: extract the inline `<script>` and run `node --check` on it. A syntax error in `index.html` takes down the whole UI, and it is invisible until the page loads.
- Report what you actually ran and what it printed. If you could not verify something, say so plainly rather than implying it passed.
