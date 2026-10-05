import pytest

from phd import errors


def test_every_error_is_a_phd_error():
    names = ["UnknownHarness", "HarnessNotInstalled", "CwdNotFound", "InputNotFound", "EmptyTask",
             "StackInvalid", "StackIncompatible", "HookUnsupported", "MissingSecret",
             "MissingCredentials", "UnsafeAutonomy", "UnsupportedFeature", "InvalidSpec",
             "InvalidRunsRoot", "NotResumable", "ModelNotSupported", "StackFetchError", "StackConflict", "StackNotFound"]
    for name in names:
        assert issubclass(getattr(errors, name), errors.PhdError), name


def test_model_not_supported_message_lists_models_and_hint():
    e = errors.ModelNotSupported("codex", "gpt-5.6-tera", ["gpt-5.6-terra", "gpt-5.6-luna"], hint="gpt-5.6-terra")
    assert str(e) == ("'gpt-5.6-tera' is not a codex model. Did you mean 'gpt-5.6-terra'? "
                      "Supported: gpt-5.6-terra, gpt-5.6-luna")
    assert e.supported == ["gpt-5.6-terra", "gpt-5.6-luna"]


def test_model_not_supported_truncates_long_lists():
    e = errors.ModelNotSupported("opencode", "x", [f"p/m{i}" for i in range(30)])
    assert str(e).endswith("p/m24 …")
    with pytest.raises(errors.PhdError):
        raise e
