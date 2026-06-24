from dowse._dist import distribution_name, pip_extra_hint


def test_pip_extra_hint_uses_distribution_name() -> None:
    assert pip_extra_hint("go") == f'pip install "{distribution_name()}[go]"'
    assert "dowse-context" in pip_extra_hint("mcp") or distribution_name() in pip_extra_hint("mcp")