from __future__ import annotations

from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "email-triage" / "SKILL.md"


def test_email_triage_skill_centralizes_agent_mail_workflow() -> None:
    text = SKILL.read_text()

    assert "Email triage is a shared construct" in text
    assert "Reusable triage behavior belongs in this skill" in text
    assert "Project-specific sender lists, labels, keywords, and escalation owners belong" in text
    assert "Config files should contain only a thin pointer to this skill" in text


def test_email_triage_skill_requires_source_linked_durable_evidence() -> None:
    text = SKILL.read_text()

    assert "stable message id" in text
    assert "Append a triage summary to the durable log or wiki" in text
    assert "Do not paste full private email bodies" in text
    assert "query window" in text
    assert "counts by category" in text
