import asyncio

from plugins.oai_tools import oai_tools_request_interceptor, parse_oai_suffixes


def test_parse_oai_max_suffix():
    assert parse_oai_suffixes("gpt-5.6-max") == ("gpt-5.6", "max", set())
    assert parse_oai_suffixes("gpt-5.6-image-max") == ("gpt-5.6", "max", {"image"})


def test_max_suffix_sets_responses_reasoning_effort():
    _, _, payload = asyncio.run(
        oai_tools_request_interceptor(
            request=None,
            engine="openai-responses",
            provider={},
            api_key=None,
            url="https://api.openai.com/v1/responses",
            headers={},
            payload={"model": "gpt-5.6-max", "input": "test"},
        )
    )

    assert payload["model"] == "gpt-5.6"
    assert payload["reasoning"] == {"effort": "max", "summary": "auto"}
