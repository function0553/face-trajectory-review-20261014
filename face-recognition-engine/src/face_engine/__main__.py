"""命令行入口：让 `python -m face_engine <子命令>` 可以直接运行。

真正的参数解析和业务逻辑都在 cli.py 里，这里只做一层转发，
以便 `python -m face_engine` 与直接调用 `main()` 的行为完全一致。
"""

from face_engine.cli import main


if __name__ == "__main__":
    # 把 main() 的返回码作为进程退出码（0 成功，非 0 失败）。
    raise SystemExit(main())
