from app.context import fit


def test_the_newest_message_survives_trimming():
    history = [
        {"role": "user", "content": "my invoice shows the wrong plan"},
        {"role": "assistant", "content": "sorry about that, checking now"},
        {"role": "user", "content": "any update?"},
    ]
    assert fit(history, budget=5)[-1]["content"] == "any update?"


def test_a_short_conversation_is_untouched():
    history = [{"role": "user", "content": "hello"}]
    assert fit(history, budget=100) == history
