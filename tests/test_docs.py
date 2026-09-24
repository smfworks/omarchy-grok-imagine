from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_skill_documents_the_http_flow() -> None:
    skill = (ROOT / ".cursor/skills/omarchy-imagine/SKILL.md").read_text(encoding="utf-8")
    docs = (ROOT / "docs/agent-skill.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for text in (skill, docs):
        assert "http://127.0.0.1:8010" in text
        assert "Bearer local-dev-token" in text
        assert "POST /api/packs" in text or "/api/packs" in text
        assert "/run" in text
        assert "/jobs" in text
        assert "/episode" in text
        assert "called_imagine_still" in text
        assert "produced_still" in text
        assert "called_imagine_video" in text
        assert "produced_mp4" in text
        assert "stitched_episode" in text
        assert "stub" in text
        assert "XAI_API_KEY" in text
    assert "grok-imagine-image-2.0" in skill
    assert "grok-imagine-video-1.5" in skill
    assert "5180" in readme
    assert "8010" in readme
    assert "last-frame" in readme.lower() or "last_frame_edit" in readme
    assert "look_bible" in readme
    assert "Grade match" in readme
    assert "look_bible" in skill
    assert "grade_match" in skill
    assert "https://console.x.ai" in readme
    assert "https://docs.x.ai/developers/model-capabilities/images/generation" in readme
    assert "https://docs.x.ai/developers/model-capabilities/video/image-to-video" in readme
    assert "MIT" in (ROOT / "LICENSE").read_text(encoding="utf-8")


def test_docs_cover_the_director_brief() -> None:
    for relative in (
        "README.md",
        ".cursor/skills/omarchy-imagine/SKILL.md",
        "docs/agent-skill.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "/api/packs/plan" in text
        assert "target_duration_sec" in text
        assert "8 to 120" in text


def test_readme_and_desktop_document_fill_and_the_app_launcher() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    desktop = (ROOT / "packaging/omarchy-grok-imagine.desktop").read_text(encoding="utf-8")
    script = (ROOT / "scripts/omarchy-grok-imagine.sh").read_text(encoding="utf-8")
    assert "/api/packs/fill" in readme
    assert "Fill blanks" in readme
    assert "Moderation retry" in readme
    assert "Generated video rejected by content moderation" in readme
    assert "omarchy-grok-imagine.sh" in readme
    assert "StartupWMClass=OmarchyGrokImagine" in desktop
    assert "Exec=omarchy-grok-imagine" in desktop
    assert "xdg-open http://127.0.0.1:5180" not in desktop
    order = [
        "omarchy-launch-or-focus-webapp",
        "omarchy-launch-webapp",
        '--app="${WEB_URL}"',
        "xdg-open",
    ]
    position = -1
    for token in order:
        found = script.find(token, position + 1)
        assert found > position, token
        position = found
    assert "OmarchyGrokImagine" in script
    assert "8010" in script
    assert "5180" in script
    assert script.startswith("#!/usr/bin/env bash\n")
