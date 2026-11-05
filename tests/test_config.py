from chess_coach.config import Settings


def test_user_agent_carries_contact_info():
    """Chess.com asks for a contactable User-Agent; assert we actually send one."""
    s = Settings(chesscom_contact="someone@example.com")
    assert s.user_agent.startswith("ChessCoach/")
    assert "someone@example.com" in s.user_agent


def test_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("ENGINE_DEPTH", "8")
    monkeypatch.setenv("CHESSCOM_RATE_PER_SEC", "0.5")
    s = Settings(_env_file=None)
    assert s.engine_depth == 8
    assert s.chesscom_rate_per_sec == 0.5


def test_log_level_is_normalised():
    assert Settings(log_level="debug").log_level == "DEBUG"
