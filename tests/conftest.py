import pytest

BOT_TOKEN = "123456789:AAEexampleexampleexampleexampleexam"


@pytest.fixture
def env():
    return {
        "ROCKETCHAT_URL": "https://chat.example.com/",
        "ROCKETCHAT_USER_ID": "rcUserId123",
        "ROCKETCHAT_TOKEN": "rcSecretToken-abcdef",
        "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        "TELEGRAM_OWNER_ID": "42",
    }


@pytest.fixture
def config(env, tmp_path):
    from telerocket.config import build_config

    env = dict(env, DATA_DIR=str(tmp_path / "data"))
    return build_config(env)
