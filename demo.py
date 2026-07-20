#!/usr/bin/env python3
"""altf demo — one driver, three live consoles.

Terminal 1:   python3 demo.py
Terminal 2:   altf watch /tmp/altf-demo/demo        (needs: pip install -e '.[watch]')
   or:        altf tail  /tmp/altf-demo/demo server
   or:        tail -f /tmp/altf-demo/demo/f1-server.raw   (zero-install)

What you'll see, on a loop:
  f1·server   a real `python -m http.server`, killed and restarted every few
              rounds so the status block screams EXITED and recovers
  f2·client   fetches pages from f1 (including a 404 that trips a watcher)
  f3·repl     a python REPL driven interactively via send() — sits AWAIT
Ctrl-C stops the loop and tears the consoles down cleanly.
"""

import asyncio
import contextlib
import itertools
import signal
import socket

from altf import LocalSpawner, Machine

WORKDIR = "/tmp/altf-demo"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def start_server(machine: Machine, port: int) -> None:
    await machine.tool_run(
        "server",
        f"python3 -u -m http.server {port} --bind 127.0.0.1",
        timeout=1.0,  # a server never returns: run() times out, console stays BUSY
    )
    banner = await machine.tool_wait("server", pattern="Serving HTTP", timeout=15)
    print(banner.splitlines()[0])


async def main() -> None:
    port = free_port()
    machine = Machine(session="demo", spawner=LocalSpawner(), workdir=WORKDIR)
    print(__doc__)
    print(f"session dir: {machine.dir}\n")

    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop.set)

    try:
        await machine.spawn("server", purpose=f"http.server :{port}", long_running=True)
        await machine.spawn("client", purpose="polls the server")
        await machine.spawn("repl", purpose="python repl via send()")
        machine.watch("server", r'" 404 -$|code 404', label="404s")

        await start_server(machine, port)
        await machine.tool_run("repl", "python3 -q -u", timeout=1.0)

        fetch = (
            f"python3 -c 'import urllib.request as u; "
            f'print("fetched", len(u.urlopen("http://127.0.0.1:{port}/").read()), "bytes")\''
        )
        fetch_missing = (
            f"python3 -c 'import urllib.request as u; "
            f'u.urlopen("http://127.0.0.1:{port}/missing")\' 2>&1 | tail -1'
        )

        for i in itertools.count(1):
            if stop.is_set():
                break
            print(f"\n━━━ round {i} ━━━")

            out = await machine.tool_run("client", fetch)
            print(f"client: {out.splitlines()[-1]}")

            out = await machine.tool_send("repl", f"{i} ** 2")
            answer = next((l for l in out.splitlines() if l.strip().isdigit()), "?")
            print(f"repl:   {i} ** 2 = {answer.strip()}")

            if i % 4 == 0:  # a 404: the server logs it, the watcher flags it
                await machine.tool_run("client", fetch_missing)

            if i % 6 == 0:  # kill the server: watch the EXITED scream, then recover
                print("pressing C-c on the server...")
                await machine.tool_press("server", ["C-c"])
                await asyncio.sleep(0.5)
                print(machine.render_status())
                print("restarting it...")
                await start_server(machine, port)

            print(machine.render_status())
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), 2.0)
    finally:
        print("\nshutting down consoles...")
        await machine.close()
        print("done.")


if __name__ == "__main__":
    asyncio.run(main())
