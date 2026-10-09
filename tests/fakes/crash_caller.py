"""A caller process for the crash-resume test: start a run whose harness reports its session id and then hangs,
print READY once the handle knows the session, then wait to be killed.

usage: crash_caller.py <stack dir> <cwd> <runs root> <fake harness path> [not-resumable]"""
import asyncio
import sys

from phd import Credentials, Dispatcher, Inputs, Spec, Stack


async def main(stack_dir: str, cwd: str, runs_root: str, fake: str, *flags: str) -> None:
    spec = Spec(harness="claude", model="haiku", stack=Stack.load(stack_dir), cwd=cwd,
                credentials=Credentials.claude_oauth_token("t"), secrets={"ECHO_PREFIX": "x"},
                resumable="not-resumable" not in flags)
    d = Dispatcher(spec, runs_root=runs_root, validate_model=False, allow_temp_runs_root=True,
                   executables={"claude": [sys.executable, fake]})
    handle = await d.start(Inputs(msg="x"))
    while handle.session_id is None:
        await asyncio.sleep(0.05)
    print("READY", flush=True)
    await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
