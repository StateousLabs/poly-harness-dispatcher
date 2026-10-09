"""A caller process for the live crash-resume contract test (B6): start a real harness run, print READY once the
handle knows the session id (the routine would save it now), then wait to be hard-killed mid-turn.

usage: live_crash_caller.py <harness> <model> <stack dir> <cwd> <runs root> <prompt>"""
import asyncio
import sys

from phd import Credentials, Dispatcher, Inputs, Spec, Stack


async def main(harness: str, model: str, stack_dir: str, cwd: str, runs_root: str, prompt: str) -> None:
    spec = Spec(harness=harness, model=model, stack=Stack.load(stack_dir), cwd=cwd,
                credentials=Credentials.from_host(harness), secrets={"ECHO_PREFIX": "phd-echo"}, timeout_s=300)
    async with Dispatcher(spec, runs_root=runs_root).run(Inputs(msg=prompt)) as handle:
        while handle.session_id is None and not handle.done:
            await asyncio.sleep(0.05)
        print(f"READY {handle.session_id}", flush=True)
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
