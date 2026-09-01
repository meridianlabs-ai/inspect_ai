from argparse import Namespace
from logging import getLogger
from pathlib import PurePosixPath
from re import Pattern, compile, match
from typing import Awaitable, Callable, Literal

from pydantic import JsonValue

from inspect_ai._util.ansi import render_text
from inspect_ai.util._sandbox import override_sandbox_output_limit, sandbox
from inspect_ai.util._sandbox.limits import SandboxEnvironmentLimits

from ..install import RECORD_SESSION_DIR
from ..state import HumanAgentState
from .command import HumanAgentCommand, call_human_agent

logger = getLogger(__name__)


class SessionEndCommand(HumanAgentCommand):
    def __init__(self, record_session: bool, user: str | None = None):
        super().__init__()
        self._record_session = record_session
        self._user = user

    @property
    def group(self) -> Literal[1, 2, 3]:
        return 1

    async def _read_session_logs(self) -> dict[str, str]:
        # retreive session logs (don't fail)
        sessions_dir = PurePosixPath(RECORD_SESSION_DIR)
        result = await sandbox().exec(
            ["ls", "-1", sessions_dir.as_posix()], user=self._user
        )
        if not result.success:
            logger.warning(f"Error listing human agent session logs: {result.stderr}")
            return {}

        # read logs
        session_logs: dict[str, str] = {}
        for session_log in result.stdout.strip().splitlines():
            try:
                limit = SandboxEnvironmentLimits.MAX_READ_FILE_SIZE
                path = (sessions_dir / session_log).as_posix()
                script = (
                    "import os,sys; "
                    "f=open(sys.argv[1], encoding='utf-8', newline=''); "
                    "size=os.fstat(f.fileno()).st_size; "
                    "size <= int(sys.argv[2]) or sys.exit('session log exceeds limit'); "
                    "sys.stdout.write(f.read())"
                )
                with override_sandbox_output_limit(limit, "exec"):
                    read_result = await sandbox().exec(
                        ["python3", "-c", script, path, str(limit)], user=self._user
                    )
                if not read_result.success:
                    raise RuntimeError(read_result.stderr)
                session_logs[session_log] = read_result.stdout
            except Exception as ex:
                logger.warning(f"Error reading human agent session log: {ex}")

        return session_logs


class QuitCommand(SessionEndCommand):
    @property
    def name(self) -> str:
        return "quit"

    @property
    def description(self) -> str:
        return "Quit the task without submitting an answer."

    def cli(self, args: Namespace) -> None:
        # verify that the user wants to proceed
        action = "quit the task without submitting an answer (ending the exercise)"
        try:
            while True:
                response = (
                    input(
                        f"\nDo you definitely want to {action}?\n\nThis will disconnect you from the task environment and you won't be able to reconnect.\n\nYes (y) or No (n): "
                    )
                    .lower()
                    .strip()
                )
                if response in ["yes", "y"]:
                    break
                elif response in ["no", "n"]:
                    return
                else:
                    print("Please enter yes or no.")
        except EOFError:
            return

        # thank the user!
        print(
            "\nThank you for working on this task!\n\n"
            + "Your task will now be scored and you will be disconnected from this container.\n"
        )

        call_human_agent("quit")

    def service(self, state: HumanAgentState) -> Callable[..., Awaitable[JsonValue]]:
        async def submit() -> None:
            if self._record_session:
                state.logs = await self._read_session_logs()
            state.running = False
            state.answer = ""

        return submit


class SubmitCommand(SessionEndCommand):
    @property
    def name(self) -> str:
        return "submit"

    @property
    def description(self) -> str:
        return "Submit your final answer for the task."

    @property
    def cli_args(self) -> list[HumanAgentCommand.CLIArg]:
        return [
            HumanAgentCommand.CLIArg(
                name="answer",
                description="Answer to submit for scoring (optional, not required for all tasks)",
            )
        ]

    def cli(self, args: Namespace) -> None:
        # read cli args
        call_args = vars(args)

        # first validate (print and exit if we get a str back)
        error = call_human_agent("validate", **call_args)
        if error:
            print(error)
            return

        # verify that the user wants to proceed
        answer = call_args.get("answer", None)
        answer_text = f" '{answer}'" if answer else ""
        action = f"end the task and submit{answer_text}"

        try:
            while True:
                response = (
                    input(
                        f"\nDo you definitely want to {action}?\n\nThis will disconnect you from the task environment and you won't be able to reconnect.\n\nYes (y) or No (n): "
                    )
                    .lower()
                    .strip()
                )
                if response in ["yes", "y"]:
                    break
                elif response in ["no", "n"]:
                    return
                else:
                    print("Please enter yes or no.")
        except EOFError:
            return

        # thank the user!
        print(
            "\nThank you for working on this task!\n\n"
            + "Your task will now be scored and you will be disconnected from this container.\n"
        )

        call_human_agent("submit", **call_args)

    def service(self, state: HumanAgentState) -> Callable[..., Awaitable[JsonValue]]:
        async def submit(answer: str) -> None:
            if self._record_session:
                state.logs = await self._read_session_logs()
            state.running = False
            state.answer = answer if answer is not None else ""

        return submit


class ValidateCommand(HumanAgentCommand):
    def __init__(self, answer: bool | str) -> None:
        self._answer = compile(answer) if isinstance(answer, str) else answer

    @property
    def name(self) -> str:
        return "validate"

    @property
    def description(self) -> str:
        return "Validate a task submission."

    @property
    def contexts(self) -> list[Literal["cli", "service"]]:
        return ["service"]

    def service(self, state: HumanAgentState) -> Callable[..., Awaitable[JsonValue]]:
        async def validate(answer: str | None) -> str | None:
            def failed(reason: str) -> str:
                return render_text(f"[bold]FAILED:[/bold] {reason}")

            if not state.running:
                return failed("Task is stopped (use 'task start' to start)")
            if self._answer:
                answer = answer.strip() if isinstance(answer, str) else answer
                if not answer:
                    return failed(
                        "An explicit answer is required for scoring this task."
                    )
                elif isinstance(self._answer, Pattern) and not match(
                    self._answer, answer
                ):
                    return failed(
                        "Your answer was not in the required format (please review the task instructions)"
                    )
            return None  # made it through verification

        return validate
