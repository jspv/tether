"""run_python: execute agent-authored Python in the sandbox and return a result dict."""

from __future__ import annotations

from dataclasses import asdict

from ..session import Session
from ..status import report_progress


def run_python(session: Session, code: str | None = None, path: str | None = None,
               args: list[str] | None = None) -> dict:
    """Run Python in the sandbox. Provide ``path`` (a script file under the root) or
    ``code`` (inline, written to a scratch script then run). Scripts may use the injected
    ``load(id)`` / ``save(id, obj)`` / ``emit(obj)`` / ``publish(path)`` helpers. Returns the
    ExecResult fields: ``stdout, stderr, result, error, exit_code, new_handles, killed_by,
    published``.
    """
    report_progress("running script in sandbox", tool="run_python")
    if path is not None:
        res = session.sandbox.run_script(path, args)
    elif code is not None:
        res = session.sandbox.run_code(code, args)
    else:
        raise ValueError("run_python requires either code= or path=")
    return asdict(res)
