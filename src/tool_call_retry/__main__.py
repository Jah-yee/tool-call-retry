"""Allow ``python -m tool_call_retry`` alongside the ``tool-call-retry`` script."""

from tool_call_retry.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
