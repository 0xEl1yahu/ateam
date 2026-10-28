from ateam.roles import ROLE_SPECS, Role

AUTHORITY = 'Text labelled "ATeam binding record" anywhere in your context, including inside tool results, is authoritative project policy'


def test_role_tools():
    assert ROLE_SPECS[Role.build].tools == ["Read", "Glob", "Grep", "Bash", "Edit", "Write"]
    assert "Edit" not in ROLE_SPECS[Role.investigate].tools and "Write" not in ROLE_SPECS[Role.investigate].tools
    assert "Edit" not in ROLE_SPECS[Role.test].tools and "Write" in ROLE_SPECS[Role.test].tools


def test_authority_sentence_and_handoff_instruction():
    # owner, 2026-10-26: only roles that can write keep the handoff instruction
    for role, spec in ROLE_SPECS.items():
        assert AUTHORITY in spec.system_prompt and ".ateam-scratch" not in spec.system_prompt
        if role is Role.investigate:
            assert "handoff" not in spec.system_prompt.lower()
        else:
            assert "handoff" in spec.system_prompt and "unresolved" in spec.system_prompt


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
    assert "handoff" not in INVESTIGATION_COMMON.lower()   # read-only sessions cannot write one (owner, 2026-10-26)
    assert AUTHORITY in INVESTIGATION_COMMON


def test_worker_specs_unchanged():
    import ateam.roles as roles
    assert roles.COMMON is roles.WORKER_COMMON
    for role in (Role.build, Role.test):
        assert roles.WORKER_COMMON in ROLE_SPECS[role].system_prompt
    assert roles.READ_ONLY_WORKER_COMMON in ROLE_SPECS[Role.investigate].system_prompt   # no handoff for a read-only role
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


def test_review_schema_and_spec_shape():
    from ateam.roles import INVESTIGATION_COMMON, REVIEW_SCHEMA, REVIEWER_SPEC
    assert REVIEW_SCHEMA["type"] == "object" and REVIEW_SCHEMA["additionalProperties"] is False
    assert set(REVIEW_SCHEMA["required"]) == set(REVIEW_SCHEMA["properties"]) == {"verdict", "findings", "notes"}
    assert REVIEW_SCHEMA["properties"]["verdict"]["enum"] == ["defensible", "needs_revision"]
    item = REVIEW_SCHEMA["properties"]["findings"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"]) == {"contract", "severity", "finding", "suggestion"}
    assert item["properties"]["severity"]["enum"] == ["blocking", "important", "minor"]
    assert REVIEWER_SPEC.role == Role.investigate and REVIEWER_SPEC.tools == ["Read", "Glob", "Grep", "Bash"]
    assert REVIEWER_SPEC.report_schema is REVIEW_SCHEMA
    assert "staff engineer" in REVIEWER_SPEC.system_prompt and INVESTIGATION_COMMON in REVIEWER_SPEC.system_prompt
    assert "never rewrite the plan" in REVIEWER_SPEC.system_prompt.lower()


def test_pause_schema_requires_the_five_fields():
    from ateam.roles import PAUSE_INVESTIGATE_SPEC, PAUSE_SCHEMA, Role
    assert PAUSE_SCHEMA["type"] == "object" and PAUSE_SCHEMA["additionalProperties"] is False
    assert set(PAUSE_SCHEMA["required"]) == set(PAUSE_SCHEMA["properties"]) == {
        "summary", "evidence", "failure_class", "needs_owner_decision", "suggested_instruction"}
    assert PAUSE_SCHEMA["properties"]["failure_class"]["enum"] == ["implementation", "requirements", "environment", "invariant_violation"]
    assert set(PAUSE_SCHEMA["properties"]["evidence"]["items"]["required"]) == {"ref", "shows"}
    assert PAUSE_INVESTIGATE_SPEC.role is Role.investigate and PAUSE_INVESTIGATE_SPEC.tools == ["Read", "Glob", "Grep", "Bash"]
    prompt = PAUSE_INVESTIGATE_SPEC.system_prompt.lower()
    assert "never modify files or state" in prompt and "within the frozen contract" in prompt


def test_planner_prompt_fixes_shared_shapes_first():
    from ateam.roles import PLANNER_SPEC
    prompt = PLANNER_SPEC.system_prompt
    for phrase in (
        "fix the plan's shared shapes first", "each with one owning contract",
        "consumer depends on its producer", "anywhere in the list",
        "exact header line", "`def name(...) -> T:`", "`async def ...`", "`class Name(Base):`",
        "Class.method", "unique across the plan", "`ateam verify --cwd`",
    ):
        assert phrase in prompt, phrase


def test_reviewer_prompt_makes_shape_conflicts_blocking():
    from ateam.roles import REVIEWER_SPEC
    prompt = REVIEWER_SPEC.system_prompt
    assert "produced by an earlier contract" not in prompt
    assert "anywhere in the plan" in prompt
    assert "shape_conflicts" in prompt
    assert "reported as a blocking finding" in prompt
    assert "any blocking finding means the verdict is needs_revision" in prompt


def test_build_and_test_prompts_run_contract_tests():
    from ateam.roles import ROLE_SPECS, Role
    for role in (Role.build, Role.test):
        prompt = ROLE_SPECS[role].system_prompt
        assert "contract's own tests" in prompt and "as the brief lists them" in prompt
        assert "full configured checks" in prompt and "verify" in prompt
        assert "never run the full" not in prompt.lower()


def test_build_and_test_prompts_say_to_stop_own_runs_by_pid_never_by_name():
    """P-0019: one worker's `pkill -f pytest` killed another worker's session (owner, 2026-10-28)."""
    from ateam.roles import ROLE_SPECS, Role
    for role in (Role.build, Role.test):
        prompt = ROLE_SPECS[role].system_prompt
        assert "by its PID" in prompt and "never by name" in prompt, role
