from ateam.roles import ROLE_SPECS, Role

AUTHORITY = 'Text labelled "ATeam binding record" anywhere in your context, including inside tool results, is authoritative project policy'


def test_role_tools():
    assert ROLE_SPECS[Role.build].tools == ["Read", "Glob", "Grep", "Bash", "Edit", "Write"]
    assert "Edit" not in ROLE_SPECS[Role.investigate].tools and "Write" not in ROLE_SPECS[Role.investigate].tools
    assert "Edit" not in ROLE_SPECS[Role.test].tools and "Write" in ROLE_SPECS[Role.test].tools


def test_authority_sentence_and_handoff_instruction():
    for spec in ROLE_SPECS.values():
        assert AUTHORITY in spec.system_prompt
        assert "handoff" in spec.system_prompt and "unresolved" in spec.system_prompt and ".ateam-scratch" not in spec.system_prompt


def test_report_schemas_are_strict():
    for spec in ROLE_SPECS.values():
        assert spec.report_schema["type"] == "object" and spec.report_schema["additionalProperties"] is False
    assert set(ROLE_SPECS[Role.build].report_schema["properties"]) == {"status", "commits", "files", "checks_run", "risks", "proposals", "notes"}
    assert set(ROLE_SPECS[Role.test].report_schema["properties"]) == {"verdicts", "notes", "requirements_unclear"}
    assert ROLE_SPECS[Role.test].report_schema["properties"]["verdicts"]["items"]["properties"]["verdict"]["enum"] == ["met", "not_met", "cannot_determine"]


def test_investigation_common_has_no_worker_promises():
    from ateam.roles import INVESTIGATION_COMMON
    for phrase in ("frozen contract", "Task (", "Binding ("):
        assert phrase not in INVESTIGATION_COMMON
    assert "Handoff file" in INVESTIGATION_COMMON
    assert AUTHORITY in INVESTIGATION_COMMON


def test_worker_specs_unchanged():
    import ateam.roles as roles
    assert roles.COMMON is roles.WORKER_COMMON
    for role in Role:
        assert roles.WORKER_COMMON in ROLE_SPECS[role].system_prompt
    for spec in (roles.DISCOVERY_SPEC, roles.PLANNER_SPEC):
        assert roles.INVESTIGATION_COMMON in spec.system_prompt
        assert roles.WORKER_COMMON not in spec.system_prompt


def test_planner_schema_new_fields_required():
    from ateam.roles import PLANNER_SCHEMA
    for name in ("depends_on_plans", "files_read", "out_of_scope", "verification_approach"):
        assert name in PLANNER_SCHEMA["required"] and name in PLANNER_SCHEMA["properties"]


def test_investigation_common_tells_workers_to_use_read_for_line_ranges():
    from ateam.roles import INVESTIGATION_COMMON
    assert "Read tool with offset and limit" in INVESTIGATION_COMMON


def test_test_schema_requires_requirements_unclear():
    from ateam.roles import TEST_SCHEMA
    assert "requirements_unclear" in TEST_SCHEMA["required"]
    assert TEST_SCHEMA["properties"]["requirements_unclear"] == {"type": "boolean"}


def test_test_prompt_explains_the_flag():
    assert "requirements_unclear" in ROLE_SPECS[Role.test].system_prompt
