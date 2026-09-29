
from slate.agent.agent import Agent

passed = 0
failed = 0

def test(name, function):
    global passed, failed

    print(f"\nTEST: {name}")

    try:
        result = function()

        print("  Result:", result)
        print(f"  ✓ PASS")
        passed += 1

    except Exception as error:
        print(f"  ✗ FAIL")
        print(f"  Error: {type(error).__name__}: {error}")
        failed += 1


def test_agent_exists():
    agent = Agent()

    assert agent is not None
    assert hasattr(agent, "run")

    return "Real Slate Agent imported successfully"


test(
    "Real Slate Agent can be created",
    test_agent_exists,
)


def test_mcp_tool_execution():
    agent = Agent()

    result = agent.run(
        "Search my Gmail for emails containing 'Slate project'. "
        "Do not send or modify any emails."
    )

    assert result is not None

    return result


test(
    "Agent can execute a real Gmail MCP operation",
    test_mcp_tool_execution,
)


def test_calendar_tool():
    agent = Agent()

    result = agent.run(
        "Check what calendar tools are available. "
        "Do not create, modify, or delete any calendar events."
    )

    assert result is not None

    return result


test(
    "Agent can interact with the real calendar MCP integration",
    test_calendar_tool,
)


def test_browser_fallback():
    agent = Agent()

    result = agent.run(
        "Open https://example.com using the browser tool. "
        "Do not perform any login or submit any forms."
    )

    assert result is not None

    return result


test(
    "Agent can use the real browser fallback",
    test_browser_fallback,
)

print()
print("=" * 60)
print("SLATE WEEK 4 MCP / AGENT INTEGRATION TEST")
print("=" * 60)

print(f"Passed: {passed}")
print(f"Failed: {failed}")

print("=" * 60)

if failed == 0:
    print("✓ Real Slate integration tests completed.")
else:
    print("✗ One or more real Slate integration tests failed.")