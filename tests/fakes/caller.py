"""A caller process for the orphan-protection test: start one run, print READY, then wait to be killed.

usage: caller.py <stack dir> <cwd> <runs root> <fake harness path>"""
import asyncio
import sys

from phd import Credentials, Dispatcher, Inputs, Spec, Stack


async def main(stack_dir: str, cwd: str, runs_root: str, fake: str) -> None:
    spec = Spec(harness="claude", model="haiku", stack=Stack.load(stack_dir), cwd=cwd,
                credentials=Credentials.claude_oauth_token("t"), secrets={"ECHO_PREFIX": "x"})
    d = Dispatcher(spec, runs_root=runs_root, validate_model=False, allow_temp_runs_root=True,
                   executables={"claude": [sys.executable, fake]})
    await d.start(Inputs(msg="x"))
    print("READY", flush=True)
    await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:5]))
