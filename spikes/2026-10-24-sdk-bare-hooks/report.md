# Spike report: SDK, bare mode, hooks, rules, worktrees
Date: 2026-10-24 23:34. Model alias: haiku. SDK: 0.2.174. 
Scratch: /private/tmp/claude-501/-Users-elibelilty-Documents-GitHub-skooly/034d3617-8c99-4966-abcd-b82a1dc4394d/scratchpad/ateam-spike

## 0. Does bare mode authenticate with claude.ai login?
ERROR ResultError: Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)
**bare mode usable with this login: False**

## 1 and 2. Structured output, and what context reaches a clean-room worker
- clean room (no settings): subtype=success is_error=False turns=3 cost=$0.002 terminal=completed tools=['Read', 'StructuredOutput'] structured_output={"status": "done", "saw_rule_token": false, "saw_claude_md_token": false, "saw_append_token": false, "saw_ups_token": false, "saw_agent_token": false, "notes": "Read src/app.py, which defines greet(name) returning f'hello {name}'. None of the five marker strings appeared in my instructions, context, or tool results, so all saw_* flags are false."}
- normal (user+project+local settings): ERROR ResultError: Claude Code returned an error result: Reached maximum budget ($0.6) (exit code: 1)
- clean room + append-system-prompt + UserPromptSubmit hook: subtype=success is_error=False turns=3 cost=$0.002 terminal=completed tools=['Read', 'StructuredOutput'] structured_output={"status": "done", "saw_rule_token": false, "saw_claude_md_token": false, "saw_append_token": true, "saw_ups_token": true, "saw_agent_token": false, "notes": "Read src/app.py (one function, greet(name) returning f'hello {name}'). APPEND-TOKEN-9052 was in the system prompt and UPS-TOKEN-2268 in the UserPromptSubmit hook context; RULE, CLAUDEMD and AGENT markers did not appear."}
- clean room + --agents/--agent build (omitClaudeMd): subtype=success is_error=False turns=2 cost=$0.002 terminal=completed tools=['Read'] structured_output=None
  init.agents=['build', 'claude', 'Explore', 'general-purpose', 'Plan', 'statusline-setup'] init.tools=['Read', 'Glob', 'Grep', 'Bash', 'Edit', 'Write']

## 3. PreToolUse hook denies edits under .ateam/
- subtype=success is_error=False turns=3 cost=$0.001 terminal=completed tools=['Edit', 'StructuredOutput'] structured_output={"status": "blocked", "edited_protected_file": false, "denial_message": "ATeam: .ateam/ is protected; contracts are read-only for workers.", "saw_rule_token": false, "saw_claude_md_token": false, "saw_ups_token": false, "saw_append_token": false, "saw_agent_token": false, "notes": "The Edit to change the title in .ateam/contracts/C-0001.yaml from 'original title' to 'HACKED' was denied with: \"ATeam: .ateam/ is protected; contracts are read-only for workers.\" Per instructions, I did not try any other tool or method and stopped. The file was not modified."}
- hook denials fired for: ['/private/tmp/claude-501/-Users-elibelilty-Documents-GitHub-skooly/034d3617-8c99-4966-abcd-b82a1dc4394d/scratchpad/ateam-spike/worktrees/deny/.ateam/contracts/C-0001.yaml']
- file after run: 'id: C-0001\ntitle: original title'
- tree: {'porcelain': '', 'head': '325acc8', 'diff_names': ''}

## 4. Stop hook blocks until a condition holds
- subtype=success is_error=False turns=3 cost=$0.002 terminal=completed tools=['Bash'] structured_output=None
- stop hook calls: [{'stop_hook_active': False, 'done_exists': False}, {'stop_hook_active': True, 'done_exists': True}]
- DONE.txt exists: True

## 5. A shell write outside declared paths is caught by the tree check
- subtype=success is_error=False turns=5 cost=$0.001 terminal=completed tools=['Bash', 'Read', 'Edit', 'StructuredOutput'] structured_output={"status": "done", "saw_rule_token": false, "saw_claude_md_token": false, "notes": "Created notes/outside.txt containing \"scratch\" using Bash (mkdir -p notes && echo scratch > notes/outside.txt). Appended \"# touched\" as a new last line of src/app.py using Edit. Nothing committed."}
- tree after run: {'porcelain': 'M src/app.py\n?? notes/outside.txt', 'head': '325acc8', 'diff_names': 'src/app.py'}
- outside declared paths: ['notes/outside.txt', 'rc/app.py']  -> scope breach detected: True

## 6. Result envelope fields actually present
```json
{
  "subtype": "success",
  "duration_ms": 3109,
  "duration_api_ms": "<set>",
  "is_error": false,
  "num_turns": 3,
  "session_id": "<set>",
  "stop_reason": "tool_use",
  "total_cost_usd": 0.00182145,
  "usage": "<set>",
  "result": "<set>",
  "structured_output": "<set>",
  "model_usage": "<set>",
  "permission_denials": null,
  "deferred_tool_use": null,
  "errors": null,
  "api_error_status": null,
  "uuid": "<set>",
  "terminal_reason": "completed",
  "origin": null
}
```
- usage keys: ['cache_creation', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'fallback_credit', 'inference_geo', 'input_tokens', 'iterations', 'output_tokens', 'output_tokens_details', 'server_tool_use', 'service_tier', 'speed']
- model_usage: {"claude-haiku-5-5": {"inputTokens": 1401, "outputTokens": 727, "cacheReadInputTokens": 6145, "cacheCreationInputTokens": 6282, "webSearchRequests": 0, "costUSD": 0.00182145, "contextWindow": 1000000, "maxOutputTokens": 128000, "thinkingTokens": 389, "canonicalModel": "claude-haiku-5-5", "provider": "firstParty", "costBasis": "list"}}
- permission callback saw 0 calls; sample: []

## 7. Two concurrent queries from one process
- a: subtype=success is_error=False turns=3 cost=$0.001 terminal=completed tools=['Read', 'StructuredOutput'] structured_output={"status": "done", "saw_rule_token": false, "saw_claude_md_token": false, "saw_append_token": false, "saw_ups_token": false, "saw_agent_token": false, "notes": "Read src/app.py successfully; it defines greet(name), which returns f'hello {name}'. None of the five marker strings appeared in my instructions, context, or tool results."}
- b: subtype=success is_error=False turns=3 cost=$0.001 terminal=completed tools=['Read', 'StructuredOutput'] structured_output={"status": "done", "saw_rule_token": false, "saw_claude_md_token": false, "saw_append_token": false, "saw_ups_token": false, "saw_agent_token": false, "notes": "Read src/app.py successfully; it defines greet(name) returning f'hello {name}'. None of the five marker strings appeared in my instructions, system prompt, injected context, or tool results."}
- wall 5.0s vs sum 9.7s; distinct sessions: True

## 8. Context and lifecycle signals: usage per message, PreCompact, resume after max_turns
- first run error: Claude Code returned an error result: Reached maximum number of turns (2) (exit code: 1)
- capped run: subtype=error_max_turns terminal=max_turns turns=3 is_error=True
- per-message usage on AssistantMessage: ['{"input_tokens": 2, "cache_creation_input_tokens": 1209, "cache_read_input_tokens": 4482, "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1209}, "output_tokens": 16, "service_tier": "standard", "inference_geo": "global", "speed": "standard"}', '{"input_tokens": 2, "cache_creation_input_tokens": 133, "cache_read_input_tokens": 5691, "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 133}, "output_tokens": 16, "service_tier": "standard", "inference_geo": "global", "speed": "standard"}']
- PreCompact fired: []
- resume of capped session: subtype=success is_error=False turns=3 cost=$0.001 terminal=completed tools=['Bash', 'Write'] structured_output=None
- handoff.yaml exists after resume: True

Total estimated cost: $0.89 across 11 runs.
