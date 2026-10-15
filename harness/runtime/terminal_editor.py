"""Small POSIX line editor for the single asynchronous terminal reader."""
from __future__ import annotations


class TerminalLineEditor:
    def __init__(self, submit, eof, redraw):
        self.submit, self.eof, self.redraw = submit, eof, redraw
        self.line = ""
        self.cursor = 0
        self.escape = ""
        self.paste = False
        self.history = []
        self.history_index = 0
        self.draft = ""

    def feed(self, text):
        for char in text:
            if self.escape:
                self.escape += char
                if self.escape == '\x1b[' or self.escape == '\x1bO': continue
                if char.isalpha() or char == '~':
                    self.command(self.escape)
                    self.escape = ""
                elif len(self.escape) > 20:
                    self.escape = ""
                continue
            if char == '\x1b': self.escape = char; continue
            if self.paste:
                self.line = self.line[:self.cursor] + char + self.line[self.cursor:]
                self.cursor += 1
                continue
            if char in {'\r', '\n'}:
                line = self.line
                self.line, self.cursor, self.draft = "", 0, ""
                self.history_index = len(self.history)
                if line.strip() and not line.startswith('/answer '):
                    self.history.append(line)
                    self.history_index = len(self.history)
                self.submit(line)
            elif char in {'\x7f', '\b'}:
                if self.cursor:
                    self.line = self.line[:self.cursor-1] + self.line[self.cursor:]
                    self.cursor -= 1
            elif char == '\x01': self.cursor = 0
            elif char == '\x05': self.cursor = len(self.line)
            elif char == '\x15': self.line, self.cursor = self.line[self.cursor:], 0
            elif char == '\x04':
                if not self.line: self.eof()
                else: self.line = self.line[:self.cursor] + self.line[self.cursor+1:]
            elif char.isprintable():
                self.line = self.line[:self.cursor] + char + self.line[self.cursor:]
                self.cursor += 1
        self.redraw()

    def command(self, sequence):
        if sequence == '\x1b[200~': self.paste = True
        elif sequence == '\x1b[201~': self.paste = False
        elif sequence in {'\x1b[D', '\x1bOD'}: self.cursor = max(0, self.cursor-1)
        elif sequence in {'\x1b[C', '\x1bOC'}: self.cursor = min(len(self.line), self.cursor+1)
        elif sequence in {'\x1b[H', '\x1bOH', '\x1b[1~'}: self.cursor = 0
        elif sequence in {'\x1b[F', '\x1bOF', '\x1b[4~'}: self.cursor = len(self.line)
        elif sequence == '\x1b[3~': self.line = self.line[:self.cursor] + self.line[self.cursor+1:]
        elif sequence in {'\x1b[A', '\x1b[B'} and self.history:
            if self.history_index == len(self.history): self.draft = self.line
            delta = -1 if sequence == '\x1b[A' else 1
            self.history_index = max(0, min(len(self.history), self.history_index+delta))
            self.line = self.history[self.history_index] if self.history_index < len(self.history) else self.draft
            self.cursor = len(self.line)
