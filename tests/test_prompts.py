from paranoia_local import prompts


class TestCodeReviewInstructions:
    def test_is_adversarial_and_agentic(self) -> None:
        t = prompts.CODE_REVIEW_INSTRUCTIONS
        assert "wrong until" in t.lower()
        # the defining upgrade: it must tell the reviewer it has the whole repo
        assert "read access" in t.lower()
        assert "call-site" in t.lower() or "call site" in t.lower()

    def test_has_all_five_sections(self) -> None:
        t = prompts.CODE_REVIEW_INSTRUCTIONS
        for heading in ("## What works", "## What doesn't work", "## Risks", "## Gaps", "## Improvements"):
            assert heading in t

    def test_has_severity_tags(self) -> None:
        t = prompts.CODE_REVIEW_INSTRUCTIONS
        for tag in ("[BLOCKER]", "[MAJOR]", "[MINOR]", "[OUT-OF-SCOPE]"):
            assert tag in t

    def test_over_engineering_is_a_defect(self) -> None:
        assert "over-engineering" in prompts.CODE_REVIEW_INSTRUCTIONS.lower()

    def test_empty_section_is_valid(self) -> None:
        assert "Nothing notable" in prompts.CODE_REVIEW_INSTRUCTIONS

    def test_forbids_running_full_suite(self) -> None:
        assert "test suite" in prompts.CODE_REVIEW_INSTRUCTIONS.lower()

    def test_intent_is_a_claim_to_verify(self) -> None:
        assert "claim" in prompts.CODE_REVIEW_INSTRUCTIONS.lower()


class TestPlanReviewInstructions:
    def test_premise_contradicted_by_code_is_top_severity(self) -> None:
        t = prompts.PLAN_REVIEW_INSTRUCTIONS.lower()
        assert "premise" in t
        assert "contradict" in t

    def test_has_fatal_tag(self) -> None:
        assert "[FATAL]" in prompts.PLAN_REVIEW_INSTRUCTIONS

    def test_uses_five_sections(self) -> None:
        for heading in ("## What works", "## What doesn't work", "## Risks", "## Gaps", "## Improvements"):
            assert heading in prompts.PLAN_REVIEW_INSTRUCTIONS


class TestCalibration:
    def test_both_reviews_honour_stakes_and_round(self) -> None:
        for t in (prompts.CODE_REVIEW_INSTRUCTIONS, prompts.PLAN_REVIEW_INSTRUCTIONS):
            assert "REVIEW CALIBRATION" in t
            assert "STAKES" in t
            assert "ROUND" in t

    def test_default_posture_is_modest_not_adversarial(self) -> None:
        # with no stakes stated, the reviewer must NOT default to max-adversarial
        assert "modest" in prompts.CODE_REVIEW_INSTRUCTIONS.lower()

    def test_high_round_declares_convergence(self) -> None:
        assert "CONVERGED" in prompts.CODE_REVIEW_INSTRUCTIONS
        assert "CONVERGED" in prompts.PLAN_REVIEW_INSTRUCTIONS

    def test_calibration_uses_only_mode_valid_severity_tags(self) -> None:
        # dogfood finding: the shared floor must not name a tag invalid for the mode —
        # [FATAL] is plan-only, [BLOCKER] is code-only.
        assert "[FATAL]" not in prompts.CODE_REVIEW_INSTRUCTIONS
        assert "[BLOCKER]" not in prompts.PLAN_REVIEW_INSTRUCTIONS

    def test_round_floor_retains_merge_blocking_major(self) -> None:
        # dogfood finding: the floor must not suppress [MAJOR] ("fix before merge").
        for t in (prompts.CODE_REVIEW_INSTRUCTIONS, prompts.PLAN_REVIEW_INSTRUCTIONS):
            assert "[MAJOR] or higher" in t

    def test_convergence_preserves_five_section_format(self) -> None:
        # dogfood finding: CONVERGED must be expressed WITHIN the five sections, not "stop".
        cal = prompts.CODE_REVIEW_INSTRUCTIONS.split("## Calibrate", 1)[1]
        assert "What doesn't work" in cal and "Nothing notable" in cal

    def test_plan_review_has_out_of_scope_tag(self) -> None:
        # the pressure valve that stops hardening becoming must-fix in plans
        assert "[OUT-OF-SCOPE]" in prompts.PLAN_REVIEW_INSTRUCTIONS

    def test_improvements_reject_hardening_beyond_stakes(self) -> None:
        # the Improvements section must route stakes-exceeding hardening to OUT-OF-SCOPE
        t = prompts.CODE_REVIEW_INSTRUCTIONS
        after = t.split("## Improvements", 1)[1]
        assert "[OUT-OF-SCOPE]" in after


class TestQueryInstructions:
    def test_direct_answer_not_five_sections(self) -> None:
        t = prompts.QUERY_INSTRUCTIONS.lower()
        assert "direct" in t
        # query mode must NOT impose the five-section scaffold
        assert "## what works" not in t

    def test_states_confidence_and_evidence(self) -> None:
        t = prompts.QUERY_INSTRUCTIONS.lower()
        assert "confidence" in t
        assert "cite" in t or "evidence" in t


class TestRebutInstructions:
    def test_concede_or_hold(self) -> None:
        t = prompts.REBUT_INSTRUCTIONS.lower()
        assert "concede" in t
        assert "hold" in t


class TestCompose:
    def test_joins_instructions_and_body(self) -> None:
        out = prompts.compose("INSTRUCTIONS", "BODY")
        assert "INSTRUCTIONS" in out
        assert "BODY" in out
        assert out.index("INSTRUCTIONS") < out.index("BODY")
