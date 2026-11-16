"""Terminal handling: alternate screen, raw-ish input, sixel output."""

from __future__ import annotations

import fcntl
import os
import select
import struct
import sys
import termios
import time
import tty

ESC = "\x1b"
CSI = ESC + "["
# Seconds to leave the blanked screen up before leaving the alternate screen.
BLANK_DELAY = 0.1

_KEYS = {
    b"\x1b[D": "left",
    b"\x1bOD": "left",
    b"\x1b[C": "right",
    b"\x1bOC": "right",
    b"\x1b[A": "up",
    b"\x1bOA": "up",
    b"\x1b[B": "down",
    b"\x1bOB": "down",
    b"\x1b[5~": "pageup",
    b"\x1b[6~": "pagedown",
    b"\x1b[H": "home",
    b"\x1b[1~": "home",
    b"\x1bOH": "home",
    b"\x1b[F": "end",
    b"\x1b[4~": "end",
    b"\x1bOF": "end",
    b" ": "space",
    b"m": "messages",
    b"M": "messages",
    b"s": "station",
    b"S": "station",
    b"h": "hazards",
    b"H": "hazards",
    b"w": "warnings",
    b"W": "warnings",
    b"l": "loop",
    b"L": "loop",
    b"t": "markers",
    b"T": "markers",
    b"k": "up",
    b"j": "down",
    b"q": "quit",
    b"Q": "quit",
    b"\x1b": "esc",
}


class Terminal:
    """Context manager that owns the TTY while the slideshow runs."""

    def __init__(self) -> None:
        self.fd_in = sys.stdin.fileno()
        self.out = sys.stdout.buffer
        self._saved: list | None = None
        self._pending = b""
        self._alt_screen = False

    def __enter__(self) -> Terminal:
        self._saved = termios.tcgetattr(self.fd_in)
        # cbreak keeps ISIG, so Ctrl-C still raises KeyboardInterrupt.
        tty.setcbreak(self.fd_in)
        return self

    def __exit__(self, *exc: object) -> None:
        self.leave_alt_screen()
        if self._saved is not None:
            termios.tcsetattr(self.fd_in, termios.TCSAFLUSH, self._saved)

    def write(self, data: str | bytes) -> None:
        self.out.write(data.encode() if isinstance(data, str) else data)
        self.out.flush()

    def enter_alt_screen(self) -> None:
        self._alt_screen = True
        self.write(f"{CSI}?1049h{CSI}?25l{CSI}H{CSI}2J")

    def leave_alt_screen(self) -> None:
        if self._alt_screen:
            self._alt_screen = False
            self.write(self.blank_screen())
            # The blank screen has to actually be displayed before switching
            # back; tmux, for one, only redraws the final state of a batch.
            time.sleep(BLANK_DELAY)
            self.write(f"{CSI}?25h{CSI}?1049l")

    def blank_screen(self) -> str:
        """Fill every cell with concealed spaces.

        Like the messages view (see cli.fill_line), this makes terminals that
        only repaint changed cells drop any sixel image still on screen.
        """
        rows, cols = self.size()[:2]
        rows, cols = rows or 24, cols or 80
        row = f"{CSI}0;8m{' ' * cols}{CSI}0m"
        return "".join(f"{CSI}{y};1H{row}" for y in range(1, rows + 1))

    def size(self) -> tuple[int, int, int, int]:
        """Return (rows, cols, pixel width, pixel height); pixels may be 0."""
        try:
            packed = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            return struct.unpack("HHHH", packed)
        except OSError:
            size = os.get_terminal_size()
            return size.lines, size.columns, 0, 0

    def supports_sixel(self, timeout: float = 1.0) -> bool | None:
        """Ask the terminal (DA1) whether it supports sixel; None if no reply."""
        self.write(f"{CSI}c")
        reply = b""
        deadline = time.monotonic() + timeout
        while not reply.endswith(b"c"):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.fd_in], [], [], remaining)[0]:
                return None
            reply += os.read(self.fd_in, 64)
        start = reply.rfind(b"\x1b[?")
        if start < 0:
            return None
        params = reply[start + 3 : -1].split(b";")
        return b"4" in params

    def read_key(self, timeout: float) -> str | None:
        """Return the next key name ("left", "space", ...) or None on timeout."""
        if not self._pending:
            if not select.select([self.fd_in], [], [], timeout)[0]:
                return None
            self._pending = os.read(self.fd_in, 64)
            # A lone ESC may be the start of a split escape sequence.
            if self._pending == b"\x1b" and select.select([self.fd_in], [], [], 0.05)[0]:
                self._pending += os.read(self.fd_in, 64)

        buf = self._pending
        if buf.startswith(b"\x1b") and len(buf) > 1:
            if buf[1:2] in (b"[", b"O"):
                # CSI/SS3: parameters, then a final byte in @..~.
                end = 2
                while end < len(buf) and not 0x40 <= buf[end] <= 0x7E:
                    end += 1
                seq, self._pending = buf[: end + 1], buf[end + 1 :]
            else:
                seq, self._pending = buf[:1], buf[1:]
        else:
            seq, self._pending = buf[:1], buf[1:]
        return _KEYS.get(seq)
