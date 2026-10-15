"""One cancellable stdin reader routes concurrent, synchronous human questions."""
from __future__ import annotations

import asyncio
import sys
import uuid
import weakref
import os
import codecs
import unicodedata
from dataclasses import dataclass

_brokers = weakref.WeakKeyDictionary()


@dataclass
class PendingQuestion:
    prompt: str
    future: asyncio.Future
    last_shown: float
    messages: int = 0
    guidance_interruptible: bool = False


@dataclass(frozen=True)
class GuidanceWake:
    """New guidance interrupted a business question; it is not an answer."""
    revision: int


class TerminalInputBroker:
    reminder_seconds = 60
    reminder_messages = 10
    minimum_interval = 15

    def __init__(self, loop, stream):
        self.loop, self.stream = loop, stream
        self.pending = {}
        self.fd = None
        self.timer = None
        self.require_ids = False
        self.task_manager = None
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.buffer = ""
        self.paste_lines = []
        self.paste_timer = None
        self.editor = None
        self.terminal_settings = None

    def show(self, key, *, reminder=False):
        item = self.pending[key]
        item.last_shown, item.messages = self.loop.time(), 0
        print(f"\n[HITL {'仍在等待' if reminder else '需要回复'} · {key}]\n{item.prompt}\n"
              f"回复: /answer {key} <你的回答>" +
              ("（目前只有此问题，也可直接输入回答）" if self.task_manager is None and len(self.pending) == 1 and not self.require_ids else
               "（请指定问题编号，避免回复到其它或已结束的问题）"), flush=True)
        self.redraw()

    def remind(self, *, output=False):
        now = self.loop.time()
        for key, item in list(self.pending.items()):
            if output:
                item.messages += 1
            elapsed = now - item.last_shown
            if elapsed >= self.minimum_interval and (elapsed >= self.reminder_seconds or
                    item.messages >= self.reminder_messages):
                self.show(key, reminder=True)

    def tick(self):
        self.timer = None
        self.remind()
        if self.pending:
            self.timer = self.loop.call_later(1, self.tick)

    def route(self, line):
        if not line:
            for item in self.pending.values():
                if not item.future.done(): item.future.set_result(None)
            return
        text = line.strip()
        if self.task_manager and not text:
            return
        if self.task_manager and not text.startswith('/answer '):
            try:
                if text == '/messages': self.task_manager.status()
                elif text in {'/pause', '/continue'}: self.task_manager.accept(text, control=True)
                elif text.startswith('/guide '): self._guide(text[7:])
                elif text.startswith('/'):
                    print('运行中可用 /guide <内容>、/answer <编号> <回答>、/messages、/pause、/continue。', flush=True)
                else: self._guide(text)
            except Exception as exc:
                print(f'[用户引导 · 未接收] {type(exc).__name__}: {exc}', flush=True)
            return
        key = next(iter(self.pending), None)
        if text.startswith('/answer '):
            parts = text.split(maxsplit=2)
            if len(parts) != 3 or parts[1] not in self.pending:
                print('请输入 /answer <待回答的问题编号> <回答>。', flush=True)
                return
            _, key, text = parts
        elif self.require_ids or len(self.pending) != 1:
            print('请用 /answer <问题编号> <回答> 指定接收者，避免并发问题或取消后的错配。', flush=True)
            return
        if not text:
            print('回答不能为空。', flush=True)
            return
        if key and not self.pending[key].future.done():
            self.pending[key].future.set_result(text)
            # Remove promptly so late output cannot repeat an answered question.
            self.pending.pop(key)

    def ready(self):
        chunk = os.read(self.fd, 65536)
        if not chunk:
            self.flush_paste()
            self.route("")
            self.stop_reader()
            return
        text = self.decoder.decode(chunk)
        if self.editor:
            self.editor.feed(text)
            return
        self.buffer += text
        # Bracketed-paste delimiters can arrive split across OS reads; retain
        # partial lines until a newline, then strip only these known delimiters.
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            line = line.replace('\x1b[200~', '').replace('\x1b[201~', '').rstrip('\r')
            self.submit_line(line)

    def submit_line(self, line):
        if self.task_manager and not self.task_manager.quiet:
            print('\r\x1b[2K', end='', flush=True)
        if self.task_manager and line and not line.startswith('/'):
            self.paste_lines.append(line)
            if self.paste_timer: self.paste_timer.cancel()
            self.paste_timer = self.loop.call_later(0.1, self.flush_paste)
        else:
            self.flush_paste()
            self.route(line + '\n')

    def redraw(self):
        if self.editor and self.task_manager and not self.task_manager.quiet:
            # Render a multiline paste compactly; keep the literal newlines in
            # the submitted text. Character editing is by Unicode code point.
            before = self.editor.line[:self.editor.cursor].replace('\n', ' ↵ ')
            after = self.editor.line[self.editor.cursor:].replace('\n', ' ↵ ')
            print('\r\x1b[2K[引导] ' + before + after, end='', flush=True)
            width = sum(0 if unicodedata.combining(c) else
                        2 if unicodedata.east_asian_width(c) in {'W', 'F'} else 1 for c in after)
            if width: print(f'\x1b[{width}D', end='', flush=True)

    def flush_paste(self):
        if self.paste_timer:
            self.paste_timer.cancel()
            self.paste_timer = None
        if self.paste_lines:
            lines, self.paste_lines = self.paste_lines, []
            self.route('\n'.join(lines))

    def _guide(self, text):
        if self.task_manager.accept(text):
            for key, item in list(self.pending.items()):
                if item.guidance_interruptible and not item.future.done():
                    item.future.set_result(GuidanceWake(self.task_manager.revision))
                    self.pending.pop(key)

    def start_reader(self):
        if self.fd is None:
            self.fd = self.stream.fileno()
            self.loop.add_reader(self.fd, self.ready)
            if self.task_manager and os.isatty(self.fd):
                import termios
                from harness.runtime.terminal_editor import TerminalLineEditor
                self.terminal_settings = termios.tcgetattr(self.fd)
                settings = termios.tcgetattr(self.fd)
                settings[3] &= ~(termios.ICANON | termios.ECHO)
                settings[6][termios.VMIN], settings[6][termios.VTIME] = 1, 0
                termios.tcsetattr(self.fd, termios.TCSANOW, settings)
                self.editor = TerminalLineEditor(self.submit_line, lambda: self.route(''), self.redraw)
                if not self.task_manager.quiet: print('\x1b[?2004h', end='', flush=True)
                self.redraw()

    def stop_reader(self):
        if self.fd is not None:
            self.loop.remove_reader(self.fd)
            if self.terminal_settings is not None:
                import termios
                termios.tcsetattr(self.fd, termios.TCSANOW, self.terminal_settings)
                self.terminal_settings = None
                if self.task_manager and not self.task_manager.quiet:
                    print('\r\x1b[2K\x1b[?2004l', end='', flush=True)
            self.fd = None

    async def read(self, prompt, question_id=None, *, guidance_interruptible=False):
        key = question_id or uuid.uuid4().hex[:10]
        if key in self.pending:
            raise ValueError('Question identity is already waiting in this terminal')
        future = self.loop.create_future()
        self.pending[key] = PendingQuestion(prompt, future, self.loop.time(), guidance_interruptible=guidance_interruptible)
        self.require_ids = self.require_ids or len(self.pending) > 1
        try:
            self.start_reader()
            self.show(key)
            if self.timer is None:
                self.timer = self.loop.call_later(1, self.tick)
            return await future
        except (NotImplementedError, OSError, ValueError):
            return None
        finally:
            self.pending.pop(key, None)
            if not self.pending:
                if self.timer:
                    self.timer.cancel()
                    self.timer = None
                if self.fd is not None and self.task_manager is None:
                    try:
                        self.loop.remove_reader(self.fd)
                    except (NotImplementedError, OSError, ValueError):
                        pass
                    if future.cancelled():
                        try:
                            import termios
                            termios.tcflush(self.fd, termios.TCIFLUSH)
                        except Exception:  # Optional POSIX cleanup, never mask cancellation.
                            pass
                    self.fd = None
                if self.task_manager is None and _brokers.get(self.loop) is self:
                    _brokers.pop(self.loop, None)


def attach_task_input(manager):
    if sys.stdin is None or not sys.stdin.isatty(): return None
    loop = asyncio.get_running_loop()
    broker = _brokers.get(loop)
    if broker is None:
        broker = _brokers[loop] = TerminalInputBroker(loop, sys.stdin)
    if broker.task_manager is not None and broker.task_manager is not manager:
        raise RuntimeError('Another task owns terminal input')
    broker.task_manager = manager
    try:
        broker.start_reader()
    except (NotImplementedError, OSError, ValueError):
        try: broker.stop_reader()
        except (NotImplementedError, OSError, ValueError): pass
        broker.task_manager = None
        _brokers.pop(loop, None)
        manager.show('本终端无法注册运行中输入；可通过 /resume 补充要求。')
        return None
    manager.show('运行中可直接输入引导；回答问题使用 /answer <编号> <回答>；/messages 查看状态。')
    return broker


def detach_task_input(broker, manager):
    if broker is None or broker.task_manager is not manager: return
    # Flush before switching back to the mode command reader. Closed manager
    # rejects late text explicitly rather than routing it to the next task.
    broker.flush_paste()
    # Drain only bytes already present, while the ended task still owns stdin.
    # They cannot become commands for an unrelated subsequent task.
    if broker.fd is not None:
        import select
        while select.select([broker.fd], [], [], 0)[0]:
            broker.ready()
            if broker.fd is None: break
        if os.isatty(broker.stream.fileno()):
            import termios
            termios.tcflush(broker.stream.fileno(), termios.TCIFLUSH)
    broker.flush_paste()
    if broker.editor and broker.editor.line.strip():
        manager.show(f'任务结束时仍在编辑，未插入；可复制后通过 /resume 补充：{broker.editor.line}')
    if broker.buffer.strip():
        manager.show(f'任务结束时还有未提交输入，未插入：{broker.buffer[:180]}')
        broker.buffer = ''
    if broker.paste_timer: broker.paste_timer.cancel()
    for question in broker.pending.values():
        if not question.future.done(): question.future.cancel()
    broker.pending.clear()
    broker.stop_reader()
    if broker.timer: broker.timer.cancel()
    _brokers.pop(broker.loop, None)
    broker.task_manager = None


def notify_terminal_output():
    """One ordinary output block, not a print line or a reminder."""
    try:
        broker = _brokers.get(asyncio.get_running_loop())
    except RuntimeError:
        return
    if broker:
        # Reprint after the current block, not between its header and body.
        broker.loop.call_soon(lambda: broker.remind(output=True))
        broker.loop.call_soon(broker.redraw)


async def read_hitl_input(page_id: str, reason: str) -> str | None:
    return await read_terminal_input(
        f"[Browser page={page_id}] {reason}\n"
        "Complete sign-in or verification in the browser, or enter feedback to resume.\n"
        "Your input enters the task context. Do not enter passwords or verification codes.")


async def read_terminal_input(prompt: str, *, question_id=None, guidance_interruptible=False) -> str | None | GuidanceWake:
    # Never consume piped task input or leave executor threads stealing later commands.
    if sys.stdin is None or not sys.stdin.isatty():
        return None
    loop = asyncio.get_running_loop()
    broker = _brokers.get(loop)
    if broker is None:
        broker = _brokers[loop] = TerminalInputBroker(loop, sys.stdin)
    return await broker.read(prompt, question_id, guidance_interruptible=guidance_interruptible)
