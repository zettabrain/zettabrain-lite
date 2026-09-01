"""Skill quality gate — validate SKILL.md content and score it."""

import re
from dataclasses import dataclass, field

import frontmatter


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

_KNOWLEDGE_LINE = re.compile(
    r"("
    r"\d+[\.,]?\d*\s*%"  # percentages
    r"|\$\s*\d"  # dollar amounts
    r"|\d{1,3}(?:,\d{3})+"  # large numbers with commas
    r"|(?:approv|sign[- ]?off|authorize)\w*\s+(?:by|from|of)\b"  # named approvers
    r"|(?:VP|CEO|CFO|CTO|COO|Director|Manager|Head of|Lead)\b"  # role titles as approvers
    r"|\b(?:never|must not|must never|prohibited|shall not|do not|don'?t)\b"  # prohibitions
    r"|\b(?:at least|at most|no (?:more|fewer|less) than|minimum|maximum|cap(?:ped)? at|floor|ceiling)\b"  # thresholds
    r"|\b\d+\s*(?:days?|hours?|weeks?|business days?|calendar days?)\b"  # time limits
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


def validate_skill(content: str) -> QualityReport:
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
    }

    return QualityReport(
        passed=len(errors) == 0,
        score=score,
        errors=errors,
        warnings=warnings,
        stats=stats,
    )
