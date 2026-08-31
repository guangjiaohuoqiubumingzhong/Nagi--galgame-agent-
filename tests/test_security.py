from nagi.security import (
    REDACTED_VALUE,
    detected_secret_env_items,
    looks_sensitive_env_name,
    redact_artifact,
    shell_env,
)


def test_sensitive_env_name_detection_matches_runtime_policy():
    assert looks_sensitive_env_name("OPENAI_API_KEY")
    assert looks_sensitive_env_name("SERVICE_TOKEN")
    assert looks_sensitive_env_name("PASSWORD")
    assert not looks_sensitive_env_name("PATH")


def test_detected_secret_env_items_include_configured_and_sensitive_names():
    env = {
        "PATH": "/bin",
        "CUSTOM_SECRET_NAME": "custom-value",
        "OPENAI_API_KEY": "api-value",
    }

    items = detected_secret_env_items(env=env, secret_env_names={"CUSTOM_SECRET_NAME"})

    assert items == [("CUSTOM_SECRET_NAME", "custom-value"), ("OPENAI_API_KEY", "api-value")]


def test_redact_artifact_recurses_through_values_and_secret_keys():
    artifact = {
        "OPENAI_API_KEY": "api-value",
        "payload": ["api-value", {"nested": "custom-value"}],
    }

    redacted = redact_artifact(
        artifact,
        env={"OPENAI_API_KEY": "api-value", "CUSTOM_SECRET_NAME": "custom-value"},
        secret_env_names={"CUSTOM_SECRET_NAME"},
    )

    assert redacted["OPENAI_API_KEY"] == REDACTED_VALUE
    assert redacted["payload"] == [REDACTED_VALUE, {"nested": REDACTED_VALUE}]


def test_shell_env_uses_allowlist_and_sets_pwd_with_path_fallback(tmp_path):
    env = {"PATH": "/usr/bin", "HOME": "/home/user", "SECRET": "nope"}

    filtered = shell_env(env=env, allowlist=("HOME",), root=tmp_path)

    assert filtered == {"HOME": "/home/user", "PWD": str(tmp_path), "PATH": "/usr/bin"}
