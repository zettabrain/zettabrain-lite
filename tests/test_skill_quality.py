"""Unit tests for the skill quality gate and corpus grounding measurement."""

import frontmatter

from zettabrain_lite.skill_drafter import (
    _KNOWLEDGE_LINE,
    _force_frontmatter_overrides,
    measure_grounding,
    normalise_skill_output,
    validate_skill,
)

# A structurally complete skill that contains no information about any business.
HOLLOW_SKILL = """---
name: generic-doc
version: 1.0.0
description: Use this when you need to generate this type of document for the organization. Retrieve relevant corpus documents and apply organizational rules to produce the output.
requires_corpus: true
temperature: 0.2
max_tokens: 2000
---

## Retrieval Order
1. Query the corpus for the topic named in the request.
2. Query the corpus for any related background material.
3. Query the corpus for prior examples of this document.

## Rules
- Never state a figure that does not appear in a retrieved document.
- Do not include information the corpus does not support.
- Must not present an assumption as an established fact.
- Never omit a requirement that the corpus marks as mandatory.
- Do not reorder the sections defined below.
- Never leave a placeholder unresolved in the final output.
- Must not contradict a statement made earlier in the same document.
- Do not use a source that the corpus marks as superseded.
- Never present a draft figure as final.
- Do not merge two sources into a single unattributed claim.

## Boundaries
- Never invent a name, date, or figure.
- Do not speculate about matters outside the retrieved material.
- Must not provide advice beyond what the corpus states.
- Never include confidential material unless explicitly requested.
- If retrieval returns nothing, state that there is insufficient data and stop.

## Output Structure

### Opening
State the subject of the document and the date it applies to.

### Body
Present each point in its own paragraph with the supporting source named.

### Closing
State the next action and who is responsible for it.

## Self-Check
- Confirm every figure traces to a retrieved document.
- Confirm no placeholder text remains.
- Confirm each section names its source.
- Confirm no section was omitted.

## Style
- Write in complete sentences.
- Use plain language.
- Keep paragraphs short.
- Prefer active voice.

## Gaps
- None identified at this time.
"""

CORPUS_RULES = [
    {"rule": "VAT is charged at 7.5% on all items", "category": "required",
     "source": "price-list", "confidence": 0.98},
    {"rule": "Corporate account discount is 12%", "category": "threshold",
     "source": "price-list", "confidence": 0.95},
    {"rule": "Custom and event work requires a minimum of 10 working days notice",
     "category": "required", "source": "terms", "confidence": 0.9},
    {"rule": "A 60 percent deposit secures event and wedding bookings",
     "category": "required", "source": "terms", "confidence": 0.9},
    {"rule": "Bulk order discount of 10% applies above NGN 500000 order value",
     "category": "threshold", "source": "price-list", "confidence": 0.92},
]

GROUNDED_SKILL = HOLLOW_SKILL.replace(
    "- Never state a figure that does not appear in a retrieved document.",
    "- VAT is charged at 7.5% on all items; never quote a different rate.\n"
    "- Corporate account discount is 12%, applied only to accounts flagged corporate.\n"
    "- Custom and event work requires a minimum of 10 working days notice.\n"
    "- A 60 percent deposit secures event and wedding bookings.\n"
    "- Bulk order discount of 10% applies above NGN 500000 order value.\n"
    "- Never state a figure that does not appear in a retrieved document.",
)


# ── Knowledge line detection ──────────────────────────────────────────────────


class TestKnowledgeLine:
    def test_bare_prohibition_is_not_knowledge(self):
        """Prohibitions have their own check; counting them here let hollow skills pass."""
        assert not _KNOWLEDGE_LINE.search("Never invent a name, date, or figure.")
        assert not _KNOWLEDGE_LINE.search("Do not speculate beyond the retrieved material.")

    def test_percentage_is_knowledge(self):
        assert _KNOWLEDGE_LINE.search("VAT is charged at 7.5% on all items.")

    def test_non_dollar_currencies_are_knowledge(self):
        assert _KNOWLEDGE_LINE.search("Delivery to the Island is NGN 8500 per drop.")
        assert _KNOWLEDGE_LINE.search("Delivery costs ₦8,500 per drop.")
        assert _KNOWLEDGE_LINE.search("The retainer is £2,400 per month.")

    def test_bare_large_number_is_knowledge(self):
        assert _KNOWLEDGE_LINE.search("Bulk discount applies above 500000.")

    def test_qualified_time_limit_is_knowledge(self):
        assert _KNOWLEDGE_LINE.search("Custom work requires a minimum of 10 working days notice.")

    def test_named_approver_is_knowledge(self):
        assert _KNOWLEDGE_LINE.search("Discounts above 5% require VP Sales approval.")


# ── Grounding measurement ─────────────────────────────────────────────────────


class TestMeasureGrounding:
    def test_no_rules_available(self):
        result = measure_grounding(HOLLOW_SKILL, [])
        assert result["rules_available"] == 0
        assert result["rules_grounded"] == 0
        assert result["ratio"] == 0.0

    def test_rules_present_are_counted(self):
        result = measure_grounding(GROUNDED_SKILL, CORPUS_RULES)
        assert result["rules_available"] == 5
        assert result["rules_grounded"] == 5
        assert result["ratio"] == 1.0
        assert result["missing"] == []

    def test_rules_dropped_are_reported(self):
        result = measure_grounding(HOLLOW_SKILL, CORPUS_RULES)
        assert result["rules_grounded"] < result["rules_available"]
        assert result["missing"]

    def test_reworded_rule_still_counts_via_figure(self):
        content = "Apply 7.5% VAT to every line on the quote."
        rule = [{"rule": "VAT is charged at 7.5% on all items", "category": "required",
                 "source": "x", "confidence": 0.9}]
        assert measure_grounding(content, rule)["rules_grounded"] == 1


# ── Quality gate ──────────────────────────────────────────────────────────────


class TestValidateSkill:
    def test_hollow_skill_cannot_score_high(self):
        """A skill with no corpus knowledge used to score 100/100."""
        report = validate_skill(HOLLOW_SKILL, [])
        assert not report.passed
        assert report.score <= 40

    def test_dropped_rules_produce_an_error(self):
        report = validate_skill(HOLLOW_SKILL, CORPUS_RULES)
        assert not report.passed
        assert any("made it into the skill" in e for e in report.errors)

    def test_grounded_skill_passes(self):
        report = validate_skill(GROUNDED_SKILL, CORPUS_RULES)
        assert report.passed
        assert report.score > 40
        assert report.stats["grounding"]["ratio"] == 1.0

    def test_grounding_reported_in_stats(self):
        report = validate_skill(GROUNDED_SKILL, CORPUS_RULES)
        assert "grounding" in report.stats
        assert report.stats["grounding"]["rules_available"] == 5

    def test_rules_argument_is_optional(self):
        """Existing callers that pass only content must keep working."""
        report = validate_skill(GROUNDED_SKILL)
        assert report.stats["grounding"]["rules_available"] == 0


# ── Markdown wrapper repair ───────────────────────────────────────────────────

FENCED_FRONTMATTER = """```yaml
name: flower-invoice
version: 0.1.0
description: Generate a pricing quote with line items, totals and payment terms from our rate card. Use this when a customer asks for a formal quote for floral services.
requires_corpus: True
source_documents:
  - flower_price_list.xlsx
```

## Rules
- Bulk order threshold is 500,000.
```"""

WHOLE_FILE_FENCED = """```markdown
---
name: flower-invoice
version: 0.1.0
description: Generate a pricing quote with line items, totals and payment terms from our rate card. Use this when a customer asks for a formal quote for floral services.
---

## Rules
- Bulk order threshold is 500,000.
```"""

BODY_WITH_CODE_BLOCK = """---
name: api-doc
version: 0.1.0
description: Generate API reference documentation from the corpus, including endpoints, request and response examples. Use this when documenting a service interface.
---

## Output Structure

Show the request like this:

```json
{"id": 1}
```

Then the response.
"""


class TestNormaliseSkillOutput:
    def test_fenced_frontmatter_becomes_real_frontmatter(self):
        """The model writes ```yaml instead of ---, so the skill saved with no name."""
        out = normalise_skill_output(FENCED_FRONTMATTER)
        meta = frontmatter.loads(out).metadata
        assert meta["name"] == "flower-invoice"
        assert meta["version"] == "0.1.0"
        assert meta["source_documents"] == ["flower_price_list.xlsx"]

    def test_trailing_orphan_fence_removed(self):
        out = normalise_skill_output(FENCED_FRONTMATTER)
        assert not out.rstrip().endswith("```")

    def test_whole_file_fence_unwrapped(self):
        out = normalise_skill_output(WHOLE_FILE_FENCED)
        assert out.startswith("---")
        assert frontmatter.loads(out).metadata["name"] == "flower-invoice"

    def test_correct_file_is_left_alone(self):
        already_fine = "---\nname: x\nversion: 1\ndescription: y\n---\n\n## Rules\n- A rule.\n"
        assert normalise_skill_output(already_fine) == already_fine.strip()

    def test_code_blocks_in_the_body_survive(self):
        """A skill may legitimately contain fenced examples; they must not be stripped."""
        out = normalise_skill_output(BODY_WITH_CODE_BLOCK)
        assert '```json' in out
        assert '{"id": 1}' in out
        assert frontmatter.loads(out).metadata["name"] == "api-doc"

    def test_empty_input(self):
        assert normalise_skill_output("") == ""


class TestForceFrontmatterOverrides:
    def test_does_not_invent_a_frontmatter_block(self):
        """Inventing one produced a file whose only metadata was the overrides."""
        out = _force_frontmatter_overrides(FENCED_FRONTMATTER, {"currency": "NGN"})
        assert frontmatter.loads(out).metadata == {}

    def test_applies_overrides_when_frontmatter_exists(self):
        fixed = normalise_skill_output(FENCED_FRONTMATTER)
        out = _force_frontmatter_overrides(fixed, {"currency": "NGN", "tax_rate": 7.5})
        meta = frontmatter.loads(out).metadata
        assert meta["currency"] == "NGN"
        assert meta["tax_rate"] == 7.5
        assert meta["name"] == "flower-invoice"


class TestRequiredFrontmatterFields:
    def test_missing_frontmatter_is_an_error(self):
        report = validate_skill("## Rules\n- A rule.\n")
        assert not report.passed
        assert any("No frontmatter block" in e for e in report.errors)

    def test_missing_name_is_an_error(self):
        content = "---\nversion: 1.0.0\ndescription: " + ("x" * 130) + "\n---\n\n## Rules\n- A rule.\n"
        report = validate_skill(content)
        assert any("missing 'name'" in e for e in report.errors)

    def test_complete_frontmatter_raises_no_field_error(self):
        report = validate_skill(GROUNDED_SKILL, CORPUS_RULES)
        assert not any("frontmatter" in e.lower() for e in report.errors)
