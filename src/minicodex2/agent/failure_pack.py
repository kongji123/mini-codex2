from __future__ import annotations

from dataclasses import asdict, dataclass

from minicodex2.decision.failure_classifier import FailureClassifier
from minicodex2.decision.types import FailureClassification
from minicodex2.verification.result import VerificationResult


@dataclass(slots=True)
class FailurePack:
    failure_type: str
    scope: str
    command: str | None
    exit_code: int | None
    first_blocking_failure: str
    relevant_output: str
    signature: str
    suggested_next_action: str | None = None

    @classmethod
    def from_verification(
        cls,
        result: VerificationResult,
        classifier: FailureClassifier | None = None,
        user_goal: str | None = None,
    ) -> "FailurePack":
        classifier = classifier or FailureClassifier()
        failed = result.first_failed_step()
        if failed is None:
            classification = FailureClassification(
                failure_type="unknown",
                scope="global",
                signature="unknown",
                first_blocking_line=result.blocked_reason or "verification failed",
                can_retry=False,
            )
            return cls(
                failure_type=classification.failure_type,
                scope=classification.scope,
                command=None,
                exit_code=None,
                first_blocking_failure=classification.first_blocking_line,
                relevant_output=result.blocked_reason or "",
                signature=classification.signature,
                suggested_next_action=None,
            )
        command_result = failed.command_result
        classification = classifier.classify(
            step=failed.step,
            command_result=command_result,
            user_goal=user_goal,
        )
        relevant = ""
        command = None
        exit_code = None
        if command_result:
            command = command_result.command
            exit_code = command_result.exit_code
            relevant = f"stdout:\n{command_result.stdout}\nstderr:\n{command_result.stderr}"[:4000]
        return cls(
            failure_type=classification.failure_type,
            scope=classification.scope,
            command=command,
            exit_code=exit_code,
            first_blocking_failure=classification.first_blocking_line,
            relevant_output=relevant,
            signature=classification.signature,
            suggested_next_action=_suggest_next_action(classification.failure_type),
        )

    def to_runtime_message(self) -> str:
        return (
            "FailurePack:\n"
            f"- failure_type: {self.failure_type}\n"
            f"- scope: {self.scope}\n"
            f"- command: {self.command}\n"
            f"- exit_code: {self.exit_code}\n"
            f"- first_blocking_failure: {self.first_blocking_failure}\n"
            f"- signature: {self.signature}\n"
            f"- suggested_next_action: {self.suggested_next_action}\n"
            f"- relevant_output:\n{self.relevant_output}"
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _suggest_next_action(failure_type: str) -> str | None:
    if failure_type == "missing_toolchain":
        return "Ask for explicit user approval to install the missing toolchain, then use install_toolchain. If installation finishes but PATH is stale, ask the user to restart the terminal/TUI and rerun verification."
    if failure_type == "missing_dependency":
        return "Check project dependency files, add the dependency if missing, install project dependencies if policy allows, then retry relevant verification."
    if failure_type == "compile_error":
        return "Fix the first compiler error, then rebuild and rerun."
    if failure_type == "test_failure":
        return "Fix the failing behavior indicated by the first failed test, then rerun tests."
    if failure_type == "runtime_error":
        return "Fix the runtime failure for the relevant command, then rerun verification."
    return None
