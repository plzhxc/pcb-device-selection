"""``python -m validation_platform`` 入口，转发到 :mod:`validation_platform.cli`。"""

from validation_platform.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
