"""로컬 AVE 클라이언트 진입점."""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
import webbrowser
from collections import deque
from collections.abc import Callable
from pathlib import Path

import pystray
import requests
import uvicorn
from PIL import Image, ImageDraw
from app.main import LOCAL_CONTROL_TOKEN

HOST = "127.0.0.1"
PORT = 8000
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = PROJECT_ROOT / "client.log"
LOG_GUI_REFRESH_MILLISECONDS = 100
LOG_BUFFER_RECORDS = 2_000
LOG_MAX_CHARACTERS = 200_000


class GuiLogHandler(logging.Handler):
    """최근 로그를 보존하고 열린 GUI 구독자에게 직접 전달한다."""

    def __init__(self, capacity: int = LOG_BUFFER_RECORDS):
        super().__init__()
        self._records: deque[str] = deque(maxlen=capacity)
        self._subscribers: set[queue.SimpleQueue[str]] = set()
        self._buffer_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record) + "\n"
            with self._buffer_lock:
                self._records.append(message)
                subscribers = tuple(self._subscribers)
            for subscriber in subscribers:
                subscriber.put(message)
        except Exception:
            self.handleError(record)

    def subscribe(self) -> tuple[queue.SimpleQueue[str], str]:
        messages: queue.SimpleQueue[str] = queue.SimpleQueue()
        with self._buffer_lock:
            self._subscribers.add(messages)
            history = "".join(self._records)
        return messages, history

    def unsubscribe(self, messages: queue.SimpleQueue[str]) -> None:
        with self._buffer_lock:
            self._subscribers.discard(messages)


class LogWindowManager:
    """메인 스레드에서 클라이언트 로그 창의 표시 상태를 관리한다."""

    def __init__(self, log_handler: GuiLogHandler):
        self.log_handler = log_handler
        self._commands: queue.Queue[tuple[str, object | None]] = queue.Queue()

    def open(self) -> None:
        self._commands.put(("show", None))

    def stop(self) -> None:
        self._commands.put(("stop", None))

    def confirm_exit(self, on_confirm: Callable[[], None]) -> None:
        self._commands.put(("confirm_exit", on_confirm))

    def run(self) -> None:
        try:
            import tkinter as tk
            from tkinter import ttk
            from PIL import ImageTk

            root = tk.Tk()
            root.withdraw()
            root.title("AVE 클라이언트 로그")
            root.geometry("780x510")
            root.minsize(510, 315)
            root.configure(background="SystemButtonFace")
            window_icon = ImageTk.PhotoImage(_create_icon_image())
            root.iconphoto(True, window_icon)

            style = ttk.Style(root)
            if "vista" in style.theme_names():
                style.theme_use("vista")

            toolbar = ttk.Frame(root, padding=(12, 10))
            toolbar.pack(fill="x")
            auto_scroll = tk.BooleanVar(value=True)
            ttk.Checkbutton(
                toolbar,
                text="새 로그 자동 추적",
                variable=auto_scroll,
            ).pack(side="left")

            content = ttk.Frame(root)
            content.pack(fill="both", expand=True, padx=12, pady=(4, 12))
            text = tk.Text(
                content,
                wrap="none",
                state="disabled",
                font=("Consolas", 10),
                background="SystemWindow",
                foreground="SystemWindowText",
                insertbackground="SystemWindowText",
                selectbackground="SystemHighlight",
                selectforeground="SystemHighlightText",
                relief="sunken",
                borderwidth=1,
                padx=6,
                pady=6,
            )
            vertical = ttk.Scrollbar(content, orient="vertical", command=text.yview)
            horizontal = ttk.Scrollbar(content, orient="horizontal", command=text.xview)

            def update_vertical(first: str, last: str) -> None:
                vertical.set(first, last)
                if float(first) <= 0.0 and float(last) >= 1.0:
                    vertical.grid_remove()
                else:
                    vertical.grid()

            def update_horizontal(first: str, last: str) -> None:
                horizontal.set(first, last)
                if float(first) <= 0.0 and float(last) >= 1.0:
                    horizontal.grid_remove()
                else:
                    horizontal.grid()

            text.configure(
                yscrollcommand=update_vertical, xscrollcommand=update_horizontal
            )
            content.columnconfigure(0, weight=1)
            content.rowconfigure(0, weight=1)
            text.grid(row=0, column=0, sticky="nsew")
            vertical.grid(row=0, column=1, sticky="ns")
            horizontal.grid(row=1, column=0, sticky="ew")
            vertical.grid_remove()
            horizontal.grid_remove()

            def copy_all() -> None:
                content_value = text.get("1.0", "end-1c")
                root.clipboard_clear()
                root.clipboard_append(content_value)

            def clear_view() -> None:
                text.configure(state="normal")
                text.delete("1.0", "end")
                text.configure(state="disabled")

            ttk.Button(toolbar, text="전체 복사", command=copy_all).pack(side="right")
            ttk.Button(toolbar, text="화면 비우기", command=clear_view).pack(
                side="right", padx=(0, 8)
            )

            messages, history = self.log_handler.subscribe()

            def show_exit_confirmation(on_confirm: Callable[[], None]) -> None:
                message = "AVE 클라이언트를 종료할까요?\n\n진행 중인 작업은 취소되며 백그라운드 프로세스도 종료됩니다."
                confirmed = False
                try:
                    import ctypes

                    # 로그 창이 최소화되어도 즉시 보이도록 소유 창을 두지 않고
                    # Windows에 전면·최상위 표시를 요청한다.
                    # MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 |
                    # MB_SETFOREGROUND | MB_TOPMOST
                    confirmed = (
                        ctypes.windll.user32.MessageBoxW(
                            0, message, "AVE 클라이언트 종료", 0x50134
                        )
                        == 6
                    )  # IDYES
                except (AttributeError, OSError):
                    from tkinter import messagebox

                    confirmed = messagebox.askyesno(
                        "AVE 클라이언트 종료",
                        message,
                        icon="warning",
                        default="no",
                    )
                if confirmed:
                    on_confirm()

            def append(content: str) -> None:
                display_content = content.rstrip("\r\n")
                if not display_content:
                    return
                text.configure(state="normal")
                if text.index("end-1c") != "1.0":
                    text.insert("end-1c", "\n")
                text.insert("end-1c", display_content)
                character_count = int(text.count("1.0", "end-1c", "chars")[0])
                excess = character_count - LOG_MAX_CHARACTERS
                if excess > 0:
                    text.delete("1.0", f"1.0+{excess}c")
                if auto_scroll.get():
                    text.see("end")
                    text.xview_moveto(0.0)
                text.configure(state="disabled")

            def append_new_logs() -> None:
                pending: list[str] = []
                while True:
                    try:
                        pending.append(messages.get_nowait())
                    except queue.Empty:
                        break
                append("".join(pending))
                while True:
                    try:
                        command, payload = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    if command == "show":
                        root.deiconify()
                        root.lift()
                        root.focus_force()
                    elif command == "stop":
                        root.destroy()
                        return
                    elif command == "confirm_exit" and callable(payload):
                        show_exit_confirmation(payload)
                root.after(LOG_GUI_REFRESH_MILLISECONDS, append_new_logs)

            root.protocol("WM_DELETE_WINDOW", root.withdraw)
            append(history)
            append_new_logs()
            root.mainloop()
        except Exception:
            logging.getLogger(__name__).exception(
                "클라이언트 로그 창을 열지 못했습니다."
            )
        finally:
            if "messages" in locals():
                self.log_handler.unsubscribe(messages)


def _create_icon_image() -> Image.Image:
    """웹 파비콘과 동일한 재생 기호 아이콘을 래스터로 만든다."""

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, 63, 63), radius=12, fill="#000000")
    draw.polygon(((23, 17), (23, 47), (49, 32)), fill="#f97316")
    return image


def _wait_for_local_server(timeout_seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _open_web_ui() -> None:
    webbrowser.open(f"http://{HOST}:{PORT}")


LOG_BUFFER = GuiLogHandler()
LOG_WINDOW = LogWindowManager(LOG_BUFFER)


def _open_logs() -> None:
    LOG_WINDOW.open()


def _shutdown(icon: pystray.Icon, server: uvicorn.Server) -> None:
    try:
        requests.post(
            f"http://{HOST}:{PORT}/api/internal/cancel-active",
            headers={"X-AVE-Local-Control": LOCAL_CONTROL_TOKEN},
            timeout=15,
        )
    except requests.RequestException:
        logging.getLogger(__name__).warning(
            "종료 전 작업 취소 요청을 전송하지 못했습니다.", exc_info=True
        )
    server.should_exit = True
    icon.stop()
    LOG_WINDOW.stop()


def _stop(icon: pystray.Icon, server: uvicorn.Server) -> None:
    LOG_WINDOW.confirm_exit(
        lambda: threading.Thread(
            target=_shutdown,
            args=(icon, server),
            name="ave-client-shutdown",
            daemon=True,
        ).start()
    )


def main() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOG_BUFFER.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, LOG_BUFFER],
        force=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            "app.main:app",
            host=HOST,
            port=PORT,
            log_level="info",
            access_log=True,
            log_config=None,
        )
    )
    threading.Thread(target=server.run, name="ave-local-api", daemon=True).start()
    if not _wait_for_local_server():
        raise RuntimeError(
            "로컬 AVE 서버를 시작하지 못했습니다. client.log를 확인하세요."
        )

    icon = pystray.Icon(
        "ave-client",
        _create_icon_image(),
        "AVE 클라이언트",
        menu=pystray.Menu(
            pystray.MenuItem("AVE 클라이언트", lambda: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("웹 UI 열기", _open_web_ui, default=True),
            pystray.MenuItem("로그 열기", _open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", lambda tray: _stop(tray, server)),
        ),
    )
    _open_web_ui()
    icon.run_detached()
    LOG_WINDOW.run()
