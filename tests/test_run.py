import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultError, ResultMessage, TextBlock, ToolUseBlock

from ateam.runner.run import WorkerResult, run_worker


def _result(**over):
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="s1",
                total_cost_usd=0.01, usage={"input_tokens": 5, "output_tokens": 3}, structured_output={"status": "done"})
    base.update(over)
    return ResultMessage(**base)


@pytest.mark.asyncio
async def test_run_worker_collects_result(monkeypatch, policy_build):
    async def fake_query(prompt, options):
        yield AssistantMessage(content=[TextBlock(text="hi"), ToolUseBlock(id="t1", name="Read", input={"file_path": "src/api/x.py"})], model="haiku")
        yield _result()

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    r = await run_worker("p", ClaudeAgentOptions(), policy_build)
    assert isinstance(r, WorkerResult)
    assert r.report == {"status": "done"} and r.session_id == "s1" and r.subtype == "success" and not r.is_error
    assert r.tool_uses == [{"tool": "Read", "input": {"file_path": "src/api/x.py"}}]
    assert r.cost_usd == 0.01 and r.usage["input_tokens"] == 5 and r.num_turns == 2 and r.error is None and r.wall_s >= 0


@pytest.mark.asyncio
async def test_run_worker_keeps_result_on_result_error(monkeypatch, policy_build):
    async def fake_query(prompt, options):
        yield _result(subtype="error_max_turns", is_error=True, structured_output=None)
        raise ResultError("boom", {"subtype": "error_max_turns"}, exit_code=1)

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    r = await run_worker("p", ClaudeAgentOptions(), policy_build)
    assert r.subtype == "error_max_turns" and r.is_error and r.report is None and r.error and "boom" in r.error


@pytest.mark.asyncio
async def test_run_worker_without_result(monkeypatch, policy_build):
    async def fake_query(prompt, options):
        yield AssistantMessage(content=[TextBlock(text="hi")], model="haiku")
        raise RuntimeError("connection lost")

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    r = await run_worker("p", ClaudeAgentOptions(), policy_build)
    assert r.subtype == "no_result" and r.is_error and r.session_id is None and "connection lost" in r.error
