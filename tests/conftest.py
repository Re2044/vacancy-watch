import pytest

from vacancy_watch.config import Config
from vacancy_watch.profile import Profile


class FakeLLM:
    """Returns queued replies in order and records the prompts it was asked."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def ask(self, prompt, schema):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        return reply(schema) if callable(reply) else reply


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path / "data", prefs=tmp_path / "prefs.md")


@pytest.fixture
def profile():
    return Profile(resume="resume", prefs="prefs", queries=["backend engineer"],
                   keywords=["backend", "python", ".net"], role_words=["engineer", "developer", "entwickler"],
                   exclude=["qa", "test", "support"], languages={"en", "ru"}, years=2,
                   levels={"junior", "middle", "unknown"})
