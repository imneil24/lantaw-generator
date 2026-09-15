from app.moderation import KeywordModerationProvider, ModerationResult


def test_allows_benign_prompt():
    provider = KeywordModerationProvider(blocklist=["bomb", "cp"])
    result = provider.check("a cat playing piano in a sunny garden")
    assert isinstance(result, ModerationResult)
    assert result.allowed is True
    assert result.reason is None


def test_blocks_flagged_keyword():
    provider = KeywordModerationProvider(blocklist=["bomb", "cp"])
    result = provider.check("how to build a bomb")
    assert result.allowed is False
    assert "bomb" in result.reason


def test_blocklist_match_is_case_insensitive():
    provider = KeywordModerationProvider(blocklist=["bomb"])
    result = provider.check("How To Build A BOMB")
    assert result.allowed is False
