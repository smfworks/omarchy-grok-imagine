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
    assert "https://console.x.ai" in readme
    assert "https://docs.x.ai/developers/model-capabilities/images/generation" in readme
    assert "https://docs.x.ai/developers/model-capabilities/video/image-to-video" in readme
    assert "MIT" in (ROOT / "LICENSE").read_text(encoding="utf-8")
