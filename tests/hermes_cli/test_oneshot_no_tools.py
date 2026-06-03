from __future__ import annotations

import sys
import types


def test_run_oneshot_no_tools_disables_config_tool_fallback(monkeypatch, capsys):
    """AGENTS-150: zero-tool classifier path must be explicit, not omitted -t."""
    from hermes_cli import oneshot

    captured = {}

    def fake_run_agent(prompt, *, model=None, provider=None, toolsets=None, use_config_toolsets=True):
        captured.update(
            {
                "prompt": prompt,
                "model": model,
                "provider": provider,
                "toolsets": toolsets,
                "use_config_toolsets": use_config_toolsets,
            }
        )
        return "OK"

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)

    rc = oneshot.run_oneshot("classify", model="m", provider="p", no_tools=True)

    assert rc == 0
    assert captured == {
        "prompt": "classify",
        "model": "m",
        "provider": "p",
        "toolsets": [],
        "use_config_toolsets": False,
    }
    assert capsys.readouterr().out == "OK\n"


def test_run_oneshot_omitted_toolsets_still_uses_config_fallback(monkeypatch):
    """Documents the old unsafe default: omitted -t is not a zero-tool proof."""
    from hermes_cli import oneshot

    captured = {}

    def fake_run_agent(prompt, *, model=None, provider=None, toolsets=None, use_config_toolsets=True):
        captured.update({"toolsets": toolsets, "use_config_toolsets": use_config_toolsets})
        return "OK"

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)

    rc = oneshot.run_oneshot("classify")

    assert rc == 0
    assert captured == {"toolsets": None, "use_config_toolsets": True}


def test_parser_accepts_top_level_and_chat_no_tools_flags():
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat_parser = build_top_level_parser()

    top = parser.parse_args(["--no-tools", "-z", "classify"])
    assert top.no_tools is True

    chat = parser.parse_args(["chat", "--no-tools", "-q", "classify"])
    assert chat.no_tools is True


def test_run_agent_no_tools_passes_empty_toolset_list_not_cli_defaults(monkeypatch):
    from hermes_cli import oneshot

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def chat(self, prompt):
            captured["prompt"] = prompt
            return "OK"

    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        types.SimpleNamespace(
            load_config=lambda: {"model": {"default": "fake-model"}, "platform_tools": {"cli": ["terminal"]}}
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.models",
        types.SimpleNamespace(detect_provider_for_model=lambda model, provider: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.runtime_provider",
        types.SimpleNamespace(
            resolve_runtime_provider=lambda **_kwargs: {
                "api_key": "k",
                "base_url": "u",
                "provider": "p",
                "api_mode": "openai",
            }
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.tools_config",
        types.SimpleNamespace(_get_platform_tools=lambda _cfg, _platform: {"terminal"}),
    )
    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=FakeAgent))

    result = oneshot._run_agent("classify", toolsets=[], use_config_toolsets=False)

    assert result == "OK"
    assert captured["enabled_toolsets"] == []
    assert captured["prompt"] == "classify"


def test_run_agent_omitted_toolsets_uses_cli_defaults(monkeypatch):
    from hermes_cli import oneshot

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def chat(self, prompt):
            return "OK"

    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        types.SimpleNamespace(load_config=lambda: {"model": {"default": "fake-model"}}),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.models",
        types.SimpleNamespace(detect_provider_for_model=lambda model, provider: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.runtime_provider",
        types.SimpleNamespace(
            resolve_runtime_provider=lambda **_kwargs: {
                "api_key": "k",
                "base_url": "u",
                "provider": "p",
                "api_mode": "openai",
            }
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.tools_config",
        types.SimpleNamespace(_get_platform_tools=lambda _cfg, _platform: {"terminal"}),
    )
    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=FakeAgent))

    result = oneshot._run_agent("classify", toolsets=None, use_config_toolsets=True)

    assert result == "OK"
    assert captured["enabled_toolsets"] == ["terminal"]
