"""Skill parser — parse YAML frontmatter + markdown skill files."""

from pathlib import Path

import frontmatter

from .models import Skill


class SkillParser:
    @staticmethod
    def parse_file(file_path: str | Path) -> Skill:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"Skill file not found: {file_path}")

        with open(file_path, "r", encoding="utf-8") as f:
            post = frontmatter.load(f)

        metadata = post.metadata
        instructions = post.content

        required_fields = ["name", "version", "description"]
        for field in required_fields:
            if not metadata.get(field):
                raise ValueError(f"Skill must have '{field}' field in frontmatter")

        if not instructions or len(instructions.strip()) < 50:
            raise ValueError("Skill instructions must be at least 50 characters")

        skill_data = {
            "name": metadata["name"],
            "version": metadata["version"],
            "description": metadata["description"],
            "instructions": instructions,
        }

        optional_fields = [
            "business_type",
            "author",
            "requires_corpus",
            "requires_discovery",
            "inputs",
            "outputs",
            "references",
            "temperature",
            "max_tokens",
            "citation_required",
            "escalation_triggers",
            "skill_type",
            "corpus_doc_types",
            "tags",
            "deprecated",
            "source_documents",
            "deterministic",
        ]
        for field in optional_fields:
            if field in metadata:
                skill_data[field] = metadata[field]

        return Skill(**skill_data)

    @staticmethod
    def validate(skill: Skill) -> tuple[bool, list[str]]:
        errors = []

        if len(skill.instructions) < 50:
            errors.append("Skill instructions too short (minimum 50 characters)")

        placeholders = ["<<FILL", "TODO", "FIXME", "TEMPLATE"]
        for placeholder in placeholders:
            if placeholder in skill.instructions:
                errors.append(f"Skill contains placeholder text: {placeholder}")

        version_parts = skill.version.split(".")
        if len(version_parts) != 3:
            errors.append(f"Version must be semantic (MAJOR.MINOR.PATCH), got: {skill.version}")

        if not 0.0 <= skill.temperature <= 2.0:
            errors.append(f"Temperature must be between 0.0 and 2.0, got: {skill.temperature}")

        if skill.max_tokens <= 0:
            errors.append(f"max_tokens must be positive, got: {skill.max_tokens}")

        return (len(errors) == 0, errors)

    @staticmethod
    def parse_and_validate(file_path: str | Path) -> Skill:
        skill = SkillParser.parse_file(file_path)
        is_valid, errors = SkillParser.validate(skill)
        if not is_valid:
            error_msg = "Skill validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            raise ValueError(error_msg)
        return skill


def load_skill(file_path: str | Path) -> Skill:
    return SkillParser.parse_and_validate(file_path)
