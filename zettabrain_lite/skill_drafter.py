"""Skill quality gate and corpus rule extraction."""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import frontmatter

log = logging.getLogger(__name__)


@dataclass
class QualityReport:
    passed: bool
    score: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "score": self.score,
            "errors": self.errors,
            "warnings": self.warnings,
            "stats": self.stats,
        }


_HOLLOW_PATTERNS = [
    re.compile(r"(?<!not )write the .+ section", re.IGNORECASE),
    re.compile(r"based on the user'?s input", re.IGNORECASE),
    re.compile(r"based on .* corpus context", re.IGNORECASE),
    re.compile(r"(?<!not )(?<!don't )(?<!do not )generate (?:a |the )?.+ section", re.IGNORECASE),
]

_PROHIBITION_WORDS = re.compile(
    r"\b(never|must not|must never|do not|don'?t|prohibited|shall not)\b",
    re.IGNORECASE,
)

# Substantive knowledge is a figure, a named authority, or a bounded limit. Prohibitions are
# deliberately NOT counted here — they have their own check, and counting them in both places
# let a skill of generic "never do X" lines satisfy the corpus-knowledge requirement outright.
_CURRENCY_CODES_RE = "USD|EUR|GBP|NGN|GHS|KES|ZAR|INR|JPY|CAD|AUD|CHF|CNY|SGD|AED"

_KNOWLEDGE_LINE = re.compile(
    r"("
    r"\d+[\.,]?\d*\s*%"  # percentages
    r"|[$€£¥₦₹]\s*\d"  # amounts written with a currency symbol
    rf"|\b(?:{_CURRENCY_CODES_RE})\s*\d"  # amounts written with an ISO code
    rf"|\d\s*(?:{_CURRENCY_CODES_RE})\b"  # amounts with a trailing ISO code
    r"|\d{1,3}(?:,\d{3})+"  # large numbers with thousands separators
    r"|\b\d{4,}\b"  # bare large numbers (8500, 500000)
    r"|(?:approv|sign[- ]?off|authorize)\w*\s+(?:by|from|of)\b"  # named approvers
    r"|(?:VP|CEO|CFO|CTO|COO|Director|Manager|Head of|Lead)\b"  # role titles as approvers
    r"|\b(?:at least|at most|no (?:more|fewer|less) than|minimum|maximum|cap(?:ped)? at)(?:\s+of)?\s+\d"  # limits
    r"|\b\d+\s*(?:\w+\s+)?(?:days?|hours?|weeks?|months?)\b"  # time limits, incl. '10 working days'
    r")",
    re.IGNORECASE,
)

_REQUIRED_SECTIONS = [
    "retrieval order",
    "rules",
    "boundaries",
    "output structure",
    "self-check",
]

_TONE_CONFLICTS = [
    ({"formal"}, {"friendly"}),
    ({"technical"}, {"plain-language", "plain language"}),
]

_FILLER_PATTERNS = [
    re.compile(r"ensure (?:the )?(?:quality|accuracy|completeness)", re.IGNORECASE),
    re.compile(r"as (?:needed|appropriate|necessary)", re.IGNORECASE),
    re.compile(r"provide (?:a )?(?:comprehensive|thorough|detailed) (?:overview|analysis|review)", re.IGNORECASE),
    re.compile(r"tailor (?:the )?(?:content|output|response) to", re.IGNORECASE),
]

_ABSTENTION_PATTERNS = re.compile(
    r"("
    r"(?:if|when|where)\s+(?:no|insufficient|limited|zero|empty)\s+(?:retrieval|context|corpus|documents?|chunks?|results?)"
    r"|(?:retrieval|context|corpus|search)\s+(?:returns?|yields?|provides?|contains?)\s+(?:nothing|no |empty|zero)"
    r"|(?:cannot|can'?t|unable to)\s+(?:find|retrieve|locate)"
    r"|(?:no (?:relevant )?(?:documents?|sources?|context|information|data) (?:found|available|returned|retrieved))"
    r"|(?:abstain|decline|refuse|state that)\b"
    r"|(?:say |respond |reply |indicate ).*(?:not enough|insufficient|no data|cannot answer)"
    r"|\[INSUFFICIENT"
    r"|INSUFFICIENT[ _]DATA"
    r")",
    re.IGNORECASE,
)


def _parse_frontmatter(content: str) -> dict:
    try:
        post = frontmatter.loads(content)
        return dict(post.metadata)
    except Exception:
        return {}


def _heading_names(content: str) -> list[str]:
    return [m.group(1).strip().lower() for m in re.finditer(r"^#{1,3}\s+(.+)$", content, re.MULTILINE)]


def _style_tones(content: str) -> set[str]:
    tones = set()
    in_style = False
    for line in content.splitlines():
        if re.match(r"^#{1,3}\s+style", line, re.IGNORECASE):
            in_style = True
            continue
        if in_style and re.match(r"^#{1,3}\s+", line):
            break
        if in_style:
            lower = line.lower()
            if "formal" in lower or "avoid contractions" in lower:
                tones.add("formal")
            if "friendly" in lower or "warm" in lower or "approachable" in lower:
                tones.add("friendly")
            if "technical" in lower or "terminology" in lower or "jargon" in lower:
                tones.add("technical")
            if "plain" in lower and ("language" in lower or "english" in lower):
                tones.add("plain-language")
    return tones


def _count_style_bullets(content: str) -> int:
    count = 0
    in_style = False
    for line in content.splitlines():
        if re.match(r"^#{1,3}\s+style", line, re.IGNORECASE):
            in_style = True
            continue
        if in_style and re.match(r"^#{1,3}\s+", line):
            break
        if in_style and re.match(r"^\s*[-*]\s+", line):
            count += 1
    return count


def _count_rule_lines(content: str) -> int:
    count = 0
    in_rules = False
    for line in content.splitlines():
        if re.match(r"^#{1,3}\s+(rules|boundaries|constraints|requirements)", line, re.IGNORECASE):
            in_rules = True
            continue
        if in_rules and re.match(r"^#{1,3}\s+", line):
            in_rules = False
            continue
        if in_rules and re.match(r"^\s*[-*]\s+\S", line):
            count += 1
    return count


def _is_slug(name: str) -> bool:
    return bool(re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", name))


_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by", "is", "are",
    "be", "must", "should", "shall", "will", "any", "all", "that", "this", "from", "at", "as",
})


def _significant_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOPWORDS}


def _numbers_in(text: str) -> set[str]:
    """Numeric tokens with thousands separators normalised away."""
    return {m.replace(",", "") for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def _rule_is_present(rule_text: str, content: str, content_numbers: set[str]) -> bool:
    """True when the substance of an extracted rule survives into the generated skill.

    A rule carrying figures is present when at least one of its figures appears. A rule with
    no figures falls back to significant-word overlap, since the model may reword it.
    """
    rule_numbers = _numbers_in(rule_text)
    if rule_numbers:
        return bool(rule_numbers & content_numbers)
    words = _significant_words(rule_text)
    if not words:
        return False
    overlap = words & _significant_words(content)
    return len(overlap) / len(words) >= 0.6


def measure_grounding(content: str, rules: list[dict] | None) -> dict:
    """Report how much of the extracted corpus knowledge reached the generated skill."""
    rules = rules or []
    content_numbers = _numbers_in(content)
    present = [r for r in rules if _rule_is_present(r.get("rule", ""), content, content_numbers)]
    missing = [r for r in rules if r not in present]
    return {
        "rules_available": len(rules),
        "rules_grounded": len(present),
        "ratio": (len(present) / len(rules)) if rules else 0.0,
        "missing": [r.get("rule", "")[:120] for r in missing[:5]],
    }


def validate_skill(content: str, rules: list[dict] | None = None) -> QualityReport:
    """Check a SKILL.md for structural completeness and, when *rules* are supplied,
    for how much of the corpus knowledge actually reached the file."""
    errors: list[str] = []
    warnings: list[str] = []
    lines = content.splitlines()
    meta = _parse_frontmatter(content)
    headings = _heading_names(content)

    # --- Blocking errors ---

    hollow_count = 0
    for line in lines:
        for pat in _HOLLOW_PATTERNS:
            if pat.search(line):
                hollow_count += 1
                break
    if hollow_count > 0:
        errors.append(f"Hollow instructions: {hollow_count} line(s) restate the section name without saying what to include")

    knowledge_lines = sum(1 for line in lines if _KNOWLEDGE_LINE.search(line))
    if knowledge_lines < 5:
        errors.append(
            f"Only {knowledge_lines} line(s) contain a threshold, figure, named approver, or prohibition (need at least 5)"
        )

    prohibition_count = sum(1 for line in lines if _PROHIBITION_WORDS.search(line))
    if prohibition_count == 0:
        errors.append("No prohibitions found (need at least one 'never', 'must not', 'do not', or 'prohibited')")

    requires_corpus = meta.get("requires_corpus", False)
    if requires_corpus and not _ABSTENTION_PATTERNS.search(content):
        errors.append("requires_corpus is true but no abstention rule for when retrieval returns nothing")

    missing_sections = [s for s in _REQUIRED_SECTIONS if s not in headings]
    if missing_sections:
        errors.append(f"Missing required section(s): {', '.join(missing_sections)}")

    tones = _style_tones(content)
    for group_a, group_b in _TONE_CONFLICTS:
        if tones & group_a and tones & group_b:
            errors.append(f"Contradictory style directives: {', '.join(tones & group_a)} conflicts with {', '.join(tones & group_b)}")

    # The save path rejects a skill missing any of these, so catch it here where the repair
    # pass can still fix it, rather than at save time with a parser message.
    if not meta:
        errors.append(
            "No frontmatter block found — the file must open with a --- block containing "
            "name, version and description"
        )
    else:
        for required in ("name", "version"):
            if not meta.get(required):
                errors.append(f"Frontmatter is missing '{required}'")

    description = meta.get("description", "")
    if len(description) < 120:
        errors.append(f"Description is only {len(description)} characters (minimum 120)")

    if len(lines) < 60:
        errors.append(f"Only {len(lines)} lines (minimum 60)")

    # --- Warnings ---

    filler_count = 0
    for line in lines:
        for pat in _FILLER_PATTERNS:
            if pat.search(line):
                filler_count += 1
                break
    if filler_count > 0:
        warnings.append(f"{filler_count} filler instruction(s) detected")

    style_bullets = _count_style_bullets(content)
    if style_bullets > 4:
        warnings.append(f"{style_bullets} style bullets (consider trimming to 4 or fewer)")

    name = meta.get("name", "")
    if name and not _is_slug(name):
        warnings.append(f"Name '{name}' is not a kebab-case slug")

    if description and not re.search(r"\b(when|use this|generate|create|produce|draft|prepare)\b", description, re.IGNORECASE):
        warnings.append("Description has no trigger phrasing (e.g. 'Use this when...', 'Generate a...')")

    rule_count = _count_rule_lines(content)
    if rule_count < 10:
        warnings.append(f"Only {rule_count} specific rule(s) (aim for 10+)")

    # --- Grounding: did the corpus knowledge actually reach the file? ---

    grounding = measure_grounding(content, rules)
    if grounding["rules_available"] and grounding["ratio"] < 0.5:
        errors.append(
            f"Only {grounding['rules_grounded']} of {grounding['rules_available']} rules found in your "
            "documents made it into the skill — the rest were dropped or reworded past recognition"
        )

    # --- Scoring ---

    score = 100
    error_weights = {
        "Hollow": 25,
        "Only": 15,
        "No prohibitions": 15,
        "requires_corpus": 10,
        "Missing required": 15,
        "Contradictory": 10,
        "Description is": 5,
    }
    error_weights["No frontmatter"] = 25
    error_weights["Frontmatter is missing"] = 15

    for err in errors:
        deducted = False
        for prefix, weight in error_weights.items():
            if err.startswith(prefix):
                score -= weight
                deducted = True
                break
        if not deducted:
            score -= 10

    for _w in warnings:
        score -= 2

    # A skill carries no organisational knowledge unless something was drawn from the corpus.
    # Structure alone cannot earn a high score — that is what made hollow skills score 100.
    if requires_corpus and grounding["rules_available"] == 0:
        score = min(score, 40)
    elif grounding["rules_available"]:
        score = min(score, 40 + int(60 * grounding["ratio"]))

    score = max(0, score)

    stats = {
        "total_lines": len(lines),
        "knowledge_lines": knowledge_lines,
        "prohibition_count": prohibition_count,
        "hollow_count": hollow_count,
        "filler_count": filler_count,
        "style_bullets": style_bullets,
        "rule_count": rule_count,
        "sections_found": [s for s in _REQUIRED_SECTIONS if s in headings],
        "sections_missing": missing_sections,
        "grounding": grounding,
    }

    return QualityReport(
        passed=len(errors) == 0,
        score=score,
        errors=errors,
        warnings=warnings,
        stats=stats,
    )


# ---------------------------------------------------------------------------
# Stage 2: corpus rule extraction
# ---------------------------------------------------------------------------

_PROBE_QUERIES = [
    "pricing thresholds rates fees costs discounts",
    "prohibitions restrictions must not never forbidden",
    "requirements mandatory required must shall",
    "compliance limitations certifications regulations",
    "terms validity expiration conditions approval authority",
    "escalation approvals sign-off authorization limits",
]

_VALID_CATEGORIES = frozenset({"threshold", "approval", "prohibition", "required", "exclusion", "disclaimer"})

_EXTRACTION_PROMPT = """You are extracting business rules from internal documents.

CONTEXT (from the organization's document library):
{context}

TASK: Extract every concrete, organization-specific rule from the context above.

Each rule must be a JSON object with these fields:
- "rule": the rule statement — quote figures, percentages, dollar amounts, and names exactly as they appear
- "category": one of: threshold, approval, prohibition, required, exclusion, disclaimer
- "source": the document or section the rule came from (use the Source header if available)
- "confidence": a float from 0.0 to 1.0 — how clearly the document states this as an enforceable rule

INSTRUCTIONS:
- Quote figures exactly. "$5,000" stays "$5,000", not "a few thousand dollars".
- Never generalize a specific number into a vague statement. "10 business days" does not become "a reasonable period".
- Exclude anything a competent professional in this field would already know. General knowledge is not a rule.
- If a passage is ambiguous, lower the confidence rather than inventing a clear rule.
- If the documents contain no extractable rules, return an empty array. Never invent rules.
- De-duplicate: if the same rule appears in multiple sources, keep the most specific version.

Return ONLY a JSON array. No commentary, no markdown fences, no explanation.
Example: [{{"rule": "Discounts above 5% require VP Sales approval", "category": "approval", "source": "rate-card", "confidence": 0.95}}]
"""


def _dedup_chunks(chunks: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for chunk in chunks:
        h = hashlib.md5(chunk.encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            result.append(chunk)
    return result


def _parse_rules_json(raw: str) -> list[dict]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return []
    text = text[start : end + 1]

    arr = json.loads(text)
    if not isinstance(arr, list):
        return []
    return arr


def _validate_rule(r: Any) -> dict | None:
    if not isinstance(r, dict):
        return None
    rule_text = r.get("rule", "")
    if not rule_text or not isinstance(rule_text, str):
        return None
    category = r.get("category", "")
    if category not in _VALID_CATEGORIES:
        category = "required"
    source = str(r.get("source", "unknown"))
    try:
        confidence = float(r.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    return {
        "rule": rule_text.strip(),
        "category": category,
        "source": source.strip(),
        "confidence": round(confidence, 2),
    }


def _dedup_rules(rules: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for r in rules:
        key = r["rule"].lower().strip()
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


def extract_rules(
    llm_fn: Callable[[str], str],
    retriever: Any,
    max_rules: int = 25,
    goal: str = "",
) -> list[dict]:
    """Pull organisation-specific rules out of the corpus.

    The fixed probes below are governance vocabulary and retrieve poorly for corpora that do
    not use it — a florist, a clinic, a single-partner law practice. Probing with the user's
    own goal first is what surfaces domain rules the fixed probes miss.
    """
    queries = list(_PROBE_QUERIES)
    if goal and goal.strip():
        queries.insert(0, goal.strip())

    all_chunks: list[str] = []
    for query in queries:
        try:
            context, _citations = retriever.get_context_for_generation(query, n_results=5)
            if context:
                all_chunks.append(context)
        except Exception:
            log.debug("Retriever query failed for %r", query, exc_info=True)
            continue

    if not all_chunks:
        return []

    unique = _dedup_chunks(all_chunks)
    combined = "\n\n---\n\n".join(unique)
    if len(combined) > 14_000:
        combined = combined[:14_000]

    prompt = _EXTRACTION_PROMPT.format(context=combined)

    try:
        raw = llm_fn(prompt)
    except Exception:
        log.debug("LLM call failed during rule extraction", exc_info=True)
        return []

    try:
        parsed = _parse_rules_json(raw)
    except (json.JSONDecodeError, ValueError):
        log.debug("Failed to parse rules JSON: %s", raw[:200] if raw else "(empty)")
        return []

    validated = []
    for r in parsed:
        v = _validate_rule(r)
        if v:
            validated.append(v)

    deduped = _dedup_rules(validated)
    deduped.sort(key=lambda r: r["confidence"], reverse=True)
    return deduped[:max_rules]


# ---------------------------------------------------------------------------
# Stage 3: skill draft generation
# ---------------------------------------------------------------------------

_GENERATOR_PROMPT = """You are writing a SKILL.md file — a structured instruction document that tells an AI model \
how to generate a specific type of output for an organization.

GOAL: {goal}

{rules_block}
{example_block}
{sections_block}

SKILL NAME (kebab-case slug for frontmatter): {name_slug}
DISPLAY TITLE: {display_name}
DESCRIPTION: {description}
TONE: {tone}
REQUIRES CORPUS: {requires_corpus}
CITATIONS: {citations}
MAX TOKENS: {max_tokens}

Write the complete SKILL.md file with YAML frontmatter and markdown body.

The file must BEGIN with a frontmatter block delimited by three hyphens, exactly like this:

---
name: {name_slug}
version: 0.1.0
description: ...
---

Do NOT write the frontmatter inside a ```yaml code block, and do NOT wrap the file in a \
code fence. The first characters of your response must be the three hyphens.

REQUIRED STRUCTURE — include ALL of these sections:
- ## Retrieval Order (how to query the corpus, numbered steps)
- ## Rules (specific, enforceable rules — every extracted rule below MUST appear here verbatim)
- ## Boundaries (what the model must never do)
- ## Output Structure (subsections with specific instructions for each)
- ## Self-Check (a checklist the model runs before returning output)
- ## Style (tone and formatting directives, 4 bullets max)

FRONTMATTER must include: name, version, description, requires_corpus, temperature, max_tokens

THE SINGLE MOST IMPORTANT RULE — never write an instruction that restates the thing it is instructing:

  FORBIDDEN: "Write the executive summary section based on the user's input."
  REQUIRED:  "Open with the client's problem in their own numbers and their deadline, not with who we are. \
State the proposed approach in three sentences and give the total figure here rather than deferring it to \
the pricing table. One page maximum."

The forbidden example changes nothing about the output. The required example changes everything. \
Every instruction you write must pass this test: would the output differ if this line were deleted? \
If not, the line must not exist.

ADDITIONAL RULES FOR GENERATION:
- Do NOT invent thresholds, dollar amounts, percentages, role titles, or approval chains. \
If a rule was not provided in the extracted rules below, do not fabricate one.
- If information is missing that the skill needs, add a ## Gaps section listing the open questions. \
Never guess.
- Include at least one prohibition in Boundaries using "never", "must not", or "do not".
- If requires_corpus is true, include an abstention rule: what to do when retrieval returns nothing.
- Write the frontmatter description yourself from the goal above. It is the only thing an agent \
reads when deciding whether to use this skill, so it must name the specific document produced and \
the situation that calls for it — at least 120 characters, with trigger phrasing ("Use this \
when...", "Generate a..."). Never pad it with generic filler such as "Retrieve relevant corpus \
documents and apply organizational rules"; a vague description makes the skill fire on the wrong \
requests.
- The file must be at least 60 lines.
- Style section: do NOT combine contradictory directives (e.g., "formal" and "friendly").

Return ONLY the complete SKILL.md content. No commentary before or after.
"""

_REPAIR_PROMPT = """The SKILL.md you generated has quality issues that must be fixed.

ERRORS:
{errors}

WARNINGS:
{warnings}

ORIGINAL SKILL:
{content}

Fix every error listed above. Pay special attention to:
- The file must start with a --- frontmatter block containing name, version and description.
  Never put the frontmatter in a ```yaml block and never wrap the file in a code fence.
- Replace any hollow instructions ("Write the X section...") with specific, actionable directives
- Add missing sections (Retrieval Order, Rules, Boundaries, Output Structure, Self-Check)
- Add prohibitions using "never", "must not", or "do not"
- Ensure the description is at least 120 characters with trigger phrasing
- Ensure the file is at least 60 lines
- Add an abstention rule if requires_corpus is true

Return ONLY the corrected SKILL.md content. No commentary before or after.
"""


def _build_rules_block(rules: list[dict]) -> str:
    if not rules:
        return "EXTRACTED RULES: None found in corpus. Do not invent any."
    lines = ["EXTRACTED RULES (from the organization's documents — include ALL of these verbatim in ## Rules):"]
    for r in rules:
        lines.append(f"- [{r['category'].upper()}] {r['rule']} (source: {r['source']}, confidence: {r['confidence']})")
    return "\n".join(lines)


def _build_example_block(example: str) -> str:
    if not example:
        return ""
    trimmed = example[:4000]
    return f"EXAMPLE OUTPUT (match this document's structure, section lengths, and conventions):\n{trimmed}"


def _build_sections_block(sections: list[str]) -> str:
    if not sections:
        return ""
    return "REQUESTED SECTIONS: " + ", ".join(sections)


def _to_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip())
    return slug.strip("-")


# Suggested tax rates by currency — used by the wizard when auto-detecting currency from price list DB
_DEFAULT_TAX_RATES: dict[str, tuple[str, float]] = {
    "NGN": ("VAT", 7.5),    # Nigeria — FIRS standard rate
    "GBP": ("VAT", 20.0),   # UK
    "GHS": ("VAT", 15.0),   # Ghana
    "KES": ("VAT", 16.0),   # Kenya
    "ZAR": ("VAT", 15.0),   # South Africa
    "INR": ("GST", 18.0),   # India standard rate
    "AUD": ("GST", 10.0),   # Australia
    "CAD": ("HST", 13.0),   # Canada (Ontario)
    # USD, EUR left at 0 — rates vary too much by jurisdiction
}


_CATEGORY_CONFIGS: dict[str, dict] = {
    "pricing": {
        "keywords": {"quote", "invoice", "pricing", "price", "billing", "estimate", "rate", "bid", "cost estimate"},
        "overrides": {"deterministic": True, "temperature": 0.0, "citation_required": True},
        "hint": (
            "PRICING SKILL: Set temperature: 0.0, deterministic: true, and citation_required: true in frontmatter. "
            "IMPORTANT — prices come from a structured database, not from corpus text extraction. "
            "The pipeline works as follows: (1) the LLM reads the customer request and identifies which products "
            "were ordered and in what quantity — it does NOT look up prices; (2) Python does a database lookup "
            "to retrieve the exact unit price for each product; (3) Python computes all totals with exact decimal "
            "arithmetic; (4) the LLM formats the final document. "
            "The skill instructions must: define the output table structure (line items, fees, taxes, grand total); "
            "specify rules for when fees apply (delivery zone, minimum order, urgency); specify discount rules "
            "by customer type or volume; specify the tax treatment; and say what to do when a product is not "
            "found in the price list (use [NEEDS INPUT], never invent a price). "
            "Set source_documents in frontmatter to the exact filename of the price list (e.g. "
            "flower_price_list.xlsx) so the skill is pinned to that price list. "
            "Do NOT include any instruction asking the model to find or extract prices from documents."
        ),
    },
    "proposal": {
        "keywords": {"proposal", "rfp", "pitch", "business case", "sow", "scope of work", "engagement"},
        "overrides": {"temperature": 0.3, "citation_required": True},
        "hint": (
            "PROPOSAL SKILL: Set temperature: 0.3 and citation_required: true in frontmatter. "
            "Proposals must persuade, not just inform. The Output Structure must include: (1) an executive "
            "summary that opens with the client's problem in their own language and states the proposed solution "
            "and total investment in the first paragraph — never open with who you are; (2) a scope section with "
            "explicit inclusions AND exclusions to prevent scope creep; (3) a timeline with milestones, dates, "
            "and deliverables at each stage; (4) a pricing summary (reference the rate card from corpus if "
            "available — do not invent rates); (5) qualifications or past work, cited from corpus; (6) terms "
            "and conditions. In Rules: every claim about capabilities must cite a corpus source. In Boundaries: "
            "never guarantee outcomes, never promise timelines not supported by the corpus, never disclose "
            "internal cost structures. The tone should be confident and specific — replace 'extensive experience' "
            "with the exact number of years or projects from the corpus."
        ),
    },
    "compliance": {
        "keywords": {"compliance", "audit", "regulatory", "legal", "contract", "risk", "security", "assessment"},
        "overrides": {"temperature": 0.1, "citation_required": True},
        "hint": (
            "COMPLIANCE / LEGAL SKILL: Set temperature: 0.1 and citation_required: true in frontmatter. "
            "Low temperature is critical — creative phrasing in compliance documents introduces ambiguity. "
            "The Output Structure must include: (1) a findings table with columns for requirement ID, "
            "description, status (compliant/non-compliant/partial/not assessed), evidence, and risk rating; "
            "(2) a risk summary sorted by severity; (3) remediation recommendations with owners and deadlines. "
            "In Rules: every compliance status must cite the specific regulation, clause, or policy section and "
            "the evidence supporting the determination. Use 'not assessed' rather than guessing when evidence is "
            "missing. In Boundaries: never state 'compliant' or 'non-compliant' without a cited source; never "
            "provide legal advice or interpret regulations beyond what the corpus explicitly states; never omit "
            "a finding to shorten the document. If the corpus does not contain the relevant regulatory "
            "framework, abstain entirely."
        ),
    },
    "technical": {
        "keywords": {
            "api", "documentation", "runbook", "sop", "procedure", "data-dictionary", "data dictionary",
            "architecture", "technical", "specification", "system design",
        },
        "overrides": {"temperature": 0.1, "citation_required": True},
        "hint": (
            "TECHNICAL SKILL: Set temperature: 0.1 and citation_required: true in frontmatter. "
            "Low temperature prevents the model from paraphrasing technical identifiers — a renamed function "
            "or wrong flag is worse than no documentation. In Rules: reference exact file paths, command names, "
            "configuration keys, and version numbers from the corpus — never paraphrase a technical identifier. "
            "Every code example must be syntactically correct for the language specified. Steps must be in "
            "executable order with explicit prerequisites at the top. In Output Structure: include a "
            "prerequisites section listing required tools, access, and versions; a step-by-step procedure where "
            "each step has exactly one action; and a troubleshooting section for common failures. For API docs: "
            "include endpoint, method, headers, request body with example, response with example, error codes, "
            "and rate limits. In Boundaries: never invent endpoint paths, parameters, or return values not in "
            "the corpus. If a technical detail is missing, use [NEEDS INPUT] rather than guessing."
        ),
    },
    "report": {
        "keywords": {
            "status", "incident", "release-notes", "change-request", "report", "post-mortem",
            "summary", "meeting", "notes", "brief",
        },
        "overrides": {"temperature": 0.2, "citation_required": True},
        "hint": (
            "REPORT / ANALYSIS SKILL: Set temperature: 0.2 and citation_required: true in frontmatter. "
            "Reports must lead with conclusions, not context. In Output Structure: open every section with the "
            "single most important finding or metric, then provide supporting detail. Include a summary table or "
            "dashboard section at the top with key metrics. Use tables for any data with 3+ comparable items. "
            "In Rules: quantify wherever possible — '3 of 5 milestones complete (60%)' not 'most milestones on "
            "track'. Separate observed facts (from corpus or user input) from analysis (your synthesis) and "
            "label each. When reporting status, use a consistent rating system (e.g. On Track / At Risk / "
            "Blocked) defined in the skill, not ad-hoc adjectives. For incident reports: include a timeline of "
            "events, root cause, impact assessment with quantified blast radius, and action items with owners. "
            "In Boundaries: never editorialize or assign blame in incident reports; never round or approximate "
            "a number from a source document."
        ),
    },
    "communication": {
        "keywords": {"email", "letter", "memo", "message", "announcement", "newsletter", "marketing", "drafter"},
        "overrides": {"temperature": 0.5},
        "hint": (
            "COMMUNICATION SKILL: Set temperature: 0.5 in frontmatter — higher temperature produces more "
            "natural language variation, which is appropriate for human-to-human communication. In Output "
            "Structure: open with the single most important point or ask — never open with pleasantries or "
            "background. Keep paragraphs to 3 sentences maximum. End with a clear, specific call to action "
            "('Reply by Friday with your approval' not 'Let me know your thoughts'). In Rules: match the "
            "formality level to the stated audience — use contractions for peers, avoid them for executives or "
            "external clients. If the corpus contains brand voice guidelines, communication templates, or "
            "signature blocks, follow them exactly. In Boundaries: never include confidential information "
            "unless the user explicitly requests it; never use filler phrases ('I hope this email finds you "
            "well', 'Please do not hesitate to contact me'); never generate a subject line longer than 60 "
            "characters."
        ),
    },
    "training": {
        "keywords": {
            "training", "onboarding", "guide", "tutorial", "knowledge-base", "knowledge base",
            "lesson", "curriculum", "handbook",
        },
        "overrides": {"temperature": 0.3, "citation_required": True},
        "hint": (
            "TRAINING / EDUCATION SKILL: Set temperature: 0.3 and citation_required: true in frontmatter. "
            "Training materials must be accurate AND approachable. In Output Structure: start with learning "
            "objectives stating what the reader will be able to DO after completing the material (not what they "
            "will 'understand'). Structure procedures as numbered steps with exactly one action per step. "
            "Include a concrete example for every abstract concept. Add a 'Common Mistakes' section listing the "
            "3-5 most frequent errors and how to avoid them. End with a knowledge check (quiz questions or "
            "practical exercises). In Rules: every procedure must match the current corpus documentation — if "
            "the corpus describes a process, use those exact steps and screenshots rather than inventing a "
            "generic version. Use progressive complexity — introduce the simplest case first, then edge cases. "
            "In Boundaries: never skip steps to save space; never assume prior knowledge unless stated in "
            "prerequisites; never use jargon without defining it on first use."
        ),
    },
}


def _detect_skill_category(name: str, goal: str) -> str | None:
    """Match a skill to a category by checking name and goal against keyword sets."""
    text = f"{name} {goal}".lower()
    for category, config in _CATEGORY_CONFIGS.items():
        if any(re.search(r"\b" + re.escape(kw) + r"\b", text) for kw in config["keywords"]):
            return category
    return None


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*$")


def _unwrap_outer_fence(lines: list[str]) -> list[str]:
    """Drop a code fence that wraps the whole document."""
    if len(lines) >= 2 and _FENCE_RE.match(lines[0]) and lines[-1].strip() == "```":
        inner = lines[1:-1]
        # Only unwrap when the fence really spans the document, i.e. nothing closes it earlier.
        if not any(line.strip() == "```" for line in inner):
            return inner
    return lines


def normalise_skill_output(content: str) -> str:
    """Repair the markdown wrappers models put around a SKILL.md.

    Models frequently emit the frontmatter as a ```yaml block instead of a --- block, or
    wrap the whole file in a fence. Left alone, python-frontmatter sees no frontmatter at
    all and the skill is saved without its name, version and description.
    """
    if not content:
        return content

    lines = _unwrap_outer_fence(content.strip().splitlines())

    # Frontmatter emitted as a fenced block at the top: convert it to a --- block.
    if lines and _FENCE_RE.match(lines[0]):
        for i in range(1, len(lines)):
            if lines[i].strip() == "```":
                block = lines[1:i]
                if any(re.match(r"^\s*name\s*:", line) for line in block):
                    lines = ["---", *block, "---", *lines[i + 1:]]
                break

    # A stray closing fence left at the end once the opener has been consumed.
    while lines and lines[-1].strip() == "```":
        lines.pop()

    return "\n".join(lines).strip()


def _force_frontmatter_overrides(content: str, overrides: dict) -> str:
    """Set frontmatter values after generation, without inventing a frontmatter block.

    If the model produced no frontmatter, writing one here would manufacture a file whose
    only metadata is the overrides — no name, no version — which then fails at save time
    with a confusing message. Leave it alone so validation reports the real problem.
    """
    try:
        post = frontmatter.loads(content)
        if not post.metadata:
            log.warning("Generated skill has no frontmatter; not injecting overrides")
            return content
        for key, value in overrides.items():
            post.metadata[key] = value
        return frontmatter.dumps(post)
    except Exception:
        log.debug("Could not patch frontmatter overrides", exc_info=True)
        return content


def generate_skill_draft(
    llm_fn: Callable[[str], str],
    goal: str,
    name: str = "",
    sections: list[str] | None = None,
    tone: list[str] | None = None,
    requires_corpus: bool = False,
    citations: bool = False,
    max_tokens: int = 2000,
    example_output: str = "",
    rules: list[dict] | None = None,
    source_documents: list[str] | None = None,
    pricing_config: dict | None = None,
    category: str | None = None,
) -> dict:
    """Generate a skill draft.

    pricing_config={'currency': 'NGN', 'tax_name': 'VAT', 'tax_rate': 7.5} is read from the price
    list and injected into frontmatter overrides for pricing skills.

    category names the document-type guidance to apply. When None it is guessed from the name and
    goal; the guess is returned so the wizard can show it and let the user correct it.
    """
    display_name = name or "Untitled Skill"
    name_slug = _to_slug(display_name)
    tone_list = tone or ["Professional"]
    sections = sections or []

    # The description is what an agent reads to decide whether to activate this skill, so
    # padding a short goal with generic filler makes routing worse, not better. Ask the model
    # to write a real one from the goal instead of appending boilerplate here.
    description = goal.strip()

    # An explicit choice from the wizard always wins over the keyword guess. "none" means the
    # user rejected the guess and wants no document-type guidance applied.
    if category == "none":
        resolved_category, category_source = None, "user"
    elif category in _CATEGORY_CONFIGS:
        resolved_category, category_source = category, "user"
    else:
        resolved_category = _detect_skill_category(display_name, goal)
        category_source = "detected" if resolved_category else "none"

    category_config = _CATEGORY_CONFIGS.get(resolved_category) if resolved_category else None

    extra_instructions = []
    if category_config:
        extra_instructions.append(category_config["hint"])
    if source_documents:
        doc_list = "\n".join(f"  - {d}" for d in source_documents)
        extra_instructions.append(
            f"SOURCE DOCUMENTS (list these in frontmatter as source_documents and add a ## Source Documents "
            f"section referencing them):\n{doc_list}"
        )

    prompt = _GENERATOR_PROMPT.format(
        goal=goal,
        rules_block=_build_rules_block(rules or []),
        example_block=_build_example_block(example_output),
        sections_block=_build_sections_block(sections),
        name_slug=name_slug,
        display_name=display_name,
        description=description,
        tone=", ".join(tone_list),
        requires_corpus=requires_corpus,
        citations=citations,
        max_tokens=max_tokens,
    )

    if extra_instructions:
        prompt += "\n\n" + "\n\n".join(extra_instructions)

    content = normalise_skill_output(llm_fn(prompt))
    if not content or len(content.strip()) < 50:
        raise ValueError("The model returned an empty or unusable response. Try again or use a different model.")

    quality = validate_skill(content, rules)

    if not quality.passed:
        repair = _REPAIR_PROMPT.format(
            errors="\n".join(f"- {e}" for e in quality.errors),
            warnings="\n".join(f"- {w}" for w in quality.warnings),
            content=content,
        )
        try:
            repaired = normalise_skill_output(llm_fn(repair))
            repaired_quality = validate_skill(repaired, rules)
            # A repair pass can regress — smaller models often drop sections while fixing one
            # error. Keep whichever version actually scores better.
            if repaired and repaired_quality.score > quality.score:
                content, quality = repaired, repaired_quality
        except Exception:
            log.debug("Repair attempt failed", exc_info=True)

    overrides = dict(category_config["overrides"]) if category_config else {}
    if pricing_config:
        # Programmatically inject currency, tax_rate, tax_name — no LLM guessing
        overrides.update(pricing_config)
    if overrides:
        content = _force_frontmatter_overrides(content, overrides)

    return {
        "content": content,
        "quality": quality.as_dict(),
        "rules": rules or [],
        "rules_found": len(rules) if rules else 0,
        "category": resolved_category or "none",
        "category_source": category_source,
        "categories": sorted(_CATEGORY_CONFIGS.keys()),
    }
