from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.proposal_evidence import Observation, diagnose


class ProposalDiagnosisTest(unittest.TestCase):
    def test_labeled_delivery_observations(self):
        loaded = {"required": True, "present": True, "resolved": True, "loaded": True}
        cases = (
            (
                "The required skill was absent",
                Observation(required=True, present=False, success=False),
                "missing_skill",
            ),
            (
                "The required skill was present but explicitly excluded from context",
                Observation(required=True, present=True, resolved=True, loaded=False, success=False),
                "unloaded_skill",
            ),
            (
                "The required skill was present but resolution failed before loading",
                Observation(required=True, present=True, resolved=False, loaded=False, success=False),
                "unloaded_skill",
            ),
            (
                "The engineer skipped a verified instruction in the loaded skill",
                Observation(**loaded, followed=False, instruction_correct=True, success=False),
                "skipped_instruction",
            ),
            (
                "Following the loaded instruction produced the wrong behavior",
                Observation(**loaded, followed=True, instruction_correct=False, success=False),
                "incorrect_instruction",
            ),
            (
                "The tool rejected the request",
                Observation(tool_failed=True, outcome_defined=True, success=False),
                "tool_failure",
            ),
            (
                "The outcome did not specify the expected behavior",
                Observation(outcome_defined=False, success=False),
                "underspecified_outcome",
            ),
            (
                "The same instruction produced the expected behavior in a comparison run",
                Observation(**loaded, followed=True, instruction_correct=True, tool_failed=False,
                            outcome_defined=True, success=True),
                "success",
            ),
        )
        for trace, observation, expected in cases:
            with self.subTest(trace=trace):
                self.assertEqual(diagnose(observation), expected)

    def test_abstains_when_the_trace_does_not_distinguish_causes(self):
        cases = (
            ("Only the failed outcome is recorded", Observation(success=False)),
            (
                "The archive records file resolution without a context or action event",
                Observation(required=True, present=True, resolved=True, success=False),
            ),
            (
                "The action is absent but the trace does not establish whether the instruction was correct",
                Observation(required=True, present=True, resolved=True, loaded=True, followed=False, success=False),
            ),
            (
                "The trace supports both a wrong instruction and a tool failure",
                Observation(required=True, present=True, resolved=True, loaded=True,
                            followed=True, instruction_correct=False, tool_failed=True, success=False),
            ),
        )
        for trace, observation in cases:
            with self.subTest(trace=trace):
                self.assertEqual(diagnose(observation), "unknown")
