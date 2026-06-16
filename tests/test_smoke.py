from synapse_core.providers.base import Provider


def test_provider_is_abstract():
    assert hasattr(Provider, "__abstractmethods__")
    assert len(Provider.__abstractmethods__) == 6
