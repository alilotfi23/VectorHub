"""``python -m app.workers`` — run the arq worker.

Equivalent to ``arq app.workers.WorkerSettings``; a convenience for local dev
and container entrypoints. Ctrl-C stops the worker (and the heartbeat loop).
"""

import asyncio

from arq.worker import run_worker

from app.workers import WorkerSettings


def main() -> None:
    # arq ships no py.typed; its loose stubs type run_worker as returning a
    # Worker, but it is async — this is the exact call arq's own CLI makes.
    asyncio.run(run_worker(WorkerSettings))  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
