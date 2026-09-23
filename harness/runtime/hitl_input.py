"""Cancellable, serialized terminal input for page-scoped HITL waits."""
import asyncio
import sys
import weakref

_locks = weakref.WeakKeyDictionary()


async def read_hitl_input(page_id: str, reason: str) -> str | None:
    return await read_terminal_input(
        f"\n[HITL page={page_id}] {reason}\n"
        "请在浏览器完成登录/验证码；或输入本轮意见并回车以解除暂停。\n"
        "输入会进入当前任务上下文，请勿输入密码或验证码。"
    )


async def read_terminal_input(prompt: str) -> str | None:
    # Never consume piped task input. No executor thread may outlive a pause
    # and steal the next CLI command after an event resumes the worker.
    if sys.stdin is None or not sys.stdin.isatty():
        return None
    loop = asyncio.get_running_loop()
    lock = _locks.setdefault(loop, asyncio.Lock())
    async with lock:
        print(prompt, flush=True)
        while True:
            future = loop.create_future()
            fd = sys.stdin.fileno()

            def ready():
                loop.remove_reader(fd)
                if not future.done():
                    future.set_result(sys.stdin.readline())

            try:
                loop.add_reader(fd, ready)
                line = await future
            except asyncio.CancelledError:
                # Discard an unfinished line when browser events win, rather
                # than feeding it to the next page's prompt or CLI task.
                try:
                    import termios
                    termios.tcflush(fd, termios.TCIFLUSH)
                except Exception:  # Optional POSIX terminal cleanup only.
                    pass
                raise
            except (NotImplementedError, OSError, ValueError):
                return None
            finally:
                try:
                    loop.remove_reader(fd)
                except (NotImplementedError, OSError, ValueError):
                    pass
            if not line:
                return None
            text = line.strip()
            if not text:
                print("空输入不会解除暂停，请填写意见。", flush=True)
                continue
            if len(text) > 8000:
                print("意见超过 8000 字符，请缩短后重新输入。", flush=True)
                continue
            return text
