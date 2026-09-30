"""공식 최신 배포 위치에서 AVE 실행 바이너리를 내려받는다."""

from __future__ import annotations

import hashlib
import os
import queue
import sys
import threading
import urllib.request
import zipfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
YTDLP_CHECKSUMS_URL = (
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
)
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_CHECKSUM_URL = FFMPEG_ZIP_URL + ".sha256"


def _application_root() -> Path:
    return Path(sys.executable).resolve().parent


ProgressCallback = Callable[[str, float, float | None], None]
TEMPORARY_FILENAMES = ("yt-dlp.exe", "ffmpeg.zip", "ffmpeg.exe", "ffprobe.exe")


class UpdateCancelled(Exception):
    pass


@contextmanager
def _temporary_workspace(temp_root: Path) -> Iterator[Path]:
    temp_root.mkdir(parents=True, exist_ok=True)
    for filename in TEMPORARY_FILENAMES:
        (temp_root / filename).unlink(missing_ok=True)
    try:
        yield temp_root
    finally:
        for filename in TEMPORARY_FILENAMES:
            (temp_root / filename).unlink(missing_ok=True)
        try:
            temp_root.rmdir()
        except OSError:
            # 업데이터가 만들지 않은 파일이 있으면 디렉터리를 보존한다.
            pass


def _download(
    url: str,
    destination: Path,
    progress: Callable[[float | None], None] | None = None,
) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "AVE-Updater/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        content_length = response.headers.get("Content-Length")
        total = (
            int(content_length) if content_length and content_length.isdigit() else 0
        )
        received = 0
        with destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                received += len(chunk)
                if progress:
                    progress(min(1.0, received / total) if total else None)


def _download_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "AVE-Updater/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8-sig")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _yt_dlp_hash(checksums: str) -> str:
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == "yt-dlp.exe":
            return parts[0].lower()
    raise RuntimeError("yt-dlp 공식 체크섬에서 yt-dlp.exe를 찾지 못했습니다.")


def _single_hash(value: str, label: str) -> str:
    candidate = value.strip().split()[0].lower() if value.strip() else ""
    if len(candidate) != 64 or any(c not in "0123456789abcdef" for c in candidate):
        raise RuntimeError(f"{label} SHA-256 응답이 올바르지 않습니다.")
    return candidate


def update(
    install_ytdlp: bool,
    install_ffmpeg: bool,
    progress: ProgressCallback | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> None:
    callback = progress or (lambda _message, _overall, _detail: None)
    is_stopped = stop_requested or (lambda: False)

    def report(message: str, overall: float, detail: float | None) -> None:
        if is_stopped():
            raise UpdateCancelled
        callback(message, overall, detail)

    ff_span = 70 if install_ytdlp else 86
    yt_start = 78 if install_ffmpeg else 2
    yt_span = 16 if install_ffmpeg else 86
    root = _application_root()
    temp_root = root / ".temp"
    with _temporary_workspace(temp_root) as temporary_root:
        yt_dlp = temporary_root / "yt-dlp.exe"
        ffmpeg_zip = temporary_root / "ffmpeg.zip"

        staged_files: dict[str, Path] = {}
        if install_ffmpeg:
            report("FFmpeg 다운로드 준비 중...", 2, 0)
            _download(
                FFMPEG_ZIP_URL,
                ffmpeg_zip,
                lambda value: report(
                    "FFmpeg 다운로드 중...",
                    2 + (value or 0) * ff_span,
                    None if value is None else value * 100,
                ),
            )
            report("FFmpeg 무결성 확인 중...", 74 if install_ytdlp else 90, None)
            expected_zip_hash = _single_hash(
                _download_text(FFMPEG_CHECKSUM_URL), "FFmpeg ZIP"
            )
            if _sha256(ffmpeg_zip) != expected_zip_hash:
                raise RuntimeError("FFmpeg ZIP SHA-256 검증에 실패했습니다.")

            report("FFmpeg 파일 추출 중...", 76 if install_ytdlp else 94, None)
            with zipfile.ZipFile(ffmpeg_zip) as archive:
                for filename in ("ffmpeg.exe", "ffprobe.exe"):
                    matches = [
                        name
                        for name in archive.namelist()
                        if name.endswith(f"/bin/{filename}")
                    ]
                    if len(matches) != 1:
                        raise RuntimeError(
                            f"FFmpeg ZIP에서 {filename}를 찾지 못했습니다."
                        )
                    staged = temporary_root / filename
                    with archive.open(matches[0]) as source, staged.open(
                        "wb"
                    ) as output:
                        while chunk := source.read(1024 * 1024):
                            if is_stopped():
                                raise UpdateCancelled
                            output.write(chunk)
                    staged_files[filename] = staged

        if install_ytdlp:
            report("yt-dlp 다운로드 준비 중...", yt_start, 0)
            _download(
                YTDLP_URL,
                yt_dlp,
                lambda value: report(
                    "yt-dlp 다운로드 중...",
                    yt_start + (value or 0) * yt_span,
                    None if value is None else value * 100,
                ),
            )
            report("yt-dlp 무결성 확인 중...", 96 if install_ffmpeg else 90, None)
            if _sha256(yt_dlp) != _yt_dlp_hash(
                _download_text(YTDLP_CHECKSUMS_URL)
            ):
                raise RuntimeError("yt-dlp.exe SHA-256 검증에 실패했습니다.")
            staged_files["yt-dlp.exe"] = yt_dlp

        report("바이너리 설치 중...", 98, None)
        for filename, staged in staged_files.items():
            target = root / "bin" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
        callback("최신 구성 요소 다운로드가 완료되었습니다.", 100, 100)


def _legacy_main() -> None:
    import tkinter as tk
    from tkinter import ttk
    from PIL import Image, ImageDraw, ImageTk

    root = tk.Tk()
    root.withdraw()
    root.title("AVE 업데이터")
    root.geometry("620x380")
    root.minsize(540, 340)
    root.resizable(False, False)
    root.configure(background="SystemButtonFace")
    icon_image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    icon_draw = ImageDraw.Draw(icon_image)
    icon_draw.rounded_rectangle((0, 0, 63, 63), radius=12, fill="#000000")
    icon_draw.rectangle((27, 12, 37, 37), fill="#f97316")
    icon_draw.polygon(((16, 33), (48, 33), (32, 51)), fill="#f97316")
    window_icon = ImageTk.PhotoImage(icon_image)
    root.iconphoto(True, window_icon)

    frame = ttk.Frame(root, padding=(32, 28))
    frame.pack(fill="both", expand=True)

    selection_frame = ttk.Frame(frame)
    selection_frame.pack(fill="both", expand=True)
    ttk.Label(
        selection_frame,
        text="최신 버전으로 다운로드할 구성 요소를 선택하세요.",
    ).pack(anchor="w")
    install_ytdlp = tk.BooleanVar(value=True)
    install_ffmpeg = tk.BooleanVar(value=True)
    component_box = ttk.Frame(
        selection_frame, padding=(18, 14), relief="solid", borderwidth=1
    )
    component_box.pack(fill="x", pady=(24, 0))
    ttk.Checkbutton(
        component_box,
        text="yt-dlp",
        variable=install_ytdlp,
    ).pack(anchor="w", pady=(0, 10))
    ttk.Checkbutton(
        component_box,
        text="FFmpeg 및 FFprobe",
        variable=install_ffmpeg,
    ).pack(anchor="w")

    selection_buttons = ttk.Frame(selection_frame)
    selection_buttons.pack(side="bottom", fill="x")
    ttk.Button(selection_buttons, text="취소", command=root.destroy).pack(side="right")
    next_button = ttk.Button(selection_buttons, text="다음")
    next_button.pack(side="right", padx=(0, 8))

    progress_frame = ttk.Frame(frame)
    status = tk.StringVar(value="최신 바이너리를 확인하고 있습니다...")
    overall = tk.DoubleVar(value=0)
    percent = tk.StringVar(value="0%")
    overall_bar = ttk.Progressbar(progress_frame, maximum=100, variable=overall)
    overall_bar.pack(fill="x")
    overall_footer = ttk.Frame(progress_frame)
    overall_footer.pack(fill="x", pady=(8, 0))
    ttk.Label(overall_footer, text="전체 진행도").pack(side="left")
    ttk.Label(overall_footer, textvariable=percent).pack(side="right")

    detail = ttk.Progressbar(progress_frame, maximum=100, mode="determinate")
    detail.pack(fill="x", pady=(28, 0))
    detail_percent = tk.StringVar(value="0%")
    detail_footer = ttk.Frame(progress_frame)
    detail_footer.pack(fill="x", pady=(8, 0))
    status_label = ttk.Label(
        detail_footer,
        textvariable=status,
        wraplength=460,
        justify="left",
    )
    status_label.pack(side="left", anchor="w", fill="x", expand=True)
    ttk.Label(detail_footer, textvariable=detail_percent).pack(
        side="right", anchor="e", padx=(16, 0)
    )
    progress_buttons = ttk.Frame(progress_frame)
    progress_buttons.pack(side="bottom", fill="x", pady=(24, 0))
    close_button = ttk.Button(progress_buttons, text="닫기", command=root.destroy, state="disabled")
    close_button.pack(side="right")
    stop_button = ttk.Button(progress_buttons, text="중지")
    stop_button.pack(side="right", padx=(0, 8))

    def update_wrap_length(event: tk.Event) -> None:
        status_label.configure(wraplength=max(320, event.width - 64))

    root.bind("<Configure>", update_wrap_length)
    root.update_idletasks()
    window_x = max(0, (root.winfo_screenwidth() - root.winfo_width()) // 2)
    window_y = max(0, (root.winfo_screenheight() - root.winfo_height()) // 2)
    root.geometry(f"+{window_x}+{window_y}")
    root.deiconify()
    events: queue.Queue[tuple[str, object]] = queue.Queue()
    stop_event = threading.Event()
    failed = False
    active_component = ""

    def report(message: str, total: float, current: float | None) -> None:
        events.put(("progress", (message, total, current)))

    def worker(selected_ytdlp: bool, selected_ffmpeg: bool) -> None:
        try:
            update(
                selected_ytdlp,
                selected_ffmpeg,
                report,
                stop_event.is_set,
            )
        except UpdateCancelled:
            events.put(("cancelled", None))
        except Exception as exc:
            events.put(("error", str(exc)))
        else:
            events.put(("done", None))

    def poll() -> None:
        nonlocal active_component, failed
        while True:
            try:
                event, payload = events.get_nowait()
            except queue.Empty:
                break
            if event == "progress":
                message, total, current = payload
                status.set(str(message))
                overall.set(float(total))
                percent.set(f"{float(total):.0f}%")
                if current is None:
                    detail_percent.set("-")
                    detail.configure(mode="indeterminate")
                    detail.start(12)
                else:
                    detail.stop()
                    detail.configure(mode="determinate")
                    detail["value"] = float(current)
                    detail_percent.set(f"{float(current):.0f}%")
            elif event == "error":
                failed = True
                detail.stop()
                detail["value"] = 0
                detail_percent.set("-")
                status.set(f"설치 실패: {payload}")
                close_button.configure(state="normal")
                stop_button.configure(state="disabled")
                root.protocol("WM_DELETE_WINDOW", root.destroy)
            elif event == "cancelled":
                detail.stop()
                detail.configure(mode="determinate")
                detail["value"] = 0
                detail_percent.set("-")
                status.set("구성 요소 다운로드를 중지했습니다.")
                close_button.configure(state="normal")
                stop_button.configure(state="disabled")
                root.protocol("WM_DELETE_WINDOW", root.destroy)
            elif event == "done":
                detail.stop()
                detail.configure(mode="determinate")
                detail["value"] = 100
                detail_percent.set("100%")
                close_button.configure(state="normal")
                stop_button.configure(state="disabled")
                root.protocol("WM_DELETE_WINDOW", root.destroy)
        if str(close_button["state"]) == "disabled":
            root.after(100, poll)

    def begin_install() -> None:
        if not install_ytdlp.get() and not install_ffmpeg.get():
            return
        selection_frame.pack_forget()
        progress_frame.pack(fill="both", expand=True)
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        threading.Thread(
            target=worker,
            args=(install_ytdlp.get(), install_ffmpeg.get()),
            name="ave-binary-update",
            daemon=True,
        ).start()
        poll()

    def stop_install() -> None:
        stop_event.set()
        stop_button.configure(state="disabled")
        status.set("구성 요소 다운로드를 중지하는 중...")

    def update_next_state(*_args: object) -> None:
        state = "normal" if install_ytdlp.get() or install_ffmpeg.get() else "disabled"
        next_button.configure(state=state)

    install_ytdlp.trace_add("write", update_next_state)
    install_ffmpeg.trace_add("write", update_next_state)
    next_button.configure(command=begin_install)
    stop_button.configure(command=stop_install)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    if failed:
        raise SystemExit(1)


def main() -> None:
    import tkinter as tk
    from tkinter import ttk
    from PIL import Image, ImageDraw, ImageTk

    root = tk.Tk()
    root.withdraw()
    root.title("AVE 업데이터")
    root.geometry("620x380")
    root.resizable(False, False)
    root.configure(background="SystemButtonFace")

    icon_image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    icon_draw = ImageDraw.Draw(icon_image)
    icon_draw.rounded_rectangle((0, 0, 63, 63), radius=12, fill="#000000")
    icon_draw.rectangle((27, 12, 37, 37), fill="#f97316")
    icon_draw.polygon(((16, 33), (48, 33), (32, 51)), fill="#f97316")
    window_icon = ImageTk.PhotoImage(icon_image)
    root.iconphoto(True, window_icon)

    default_background = root.cget("background")
    active_background = "#dbeafe"
    content = ttk.Frame(root, padding=(32, 28))
    content.pack(fill="both", expand=True)
    instruction_label = ttk.Label(
        content, text="최신 버전으로 다운로드할 구성 요소를 선택하세요."
    )
    instruction_label.pack(anchor="w")

    component_box = tk.Frame(
        content,
        background=default_background,
        highlightbackground="#a0a0a0",
        highlightthickness=1,
        padx=12,
        pady=10,
    )
    component_box.pack(fill="x", pady=(24, 0))

    install_ytdlp = tk.BooleanVar(value=True)
    install_ffmpeg = tk.BooleanVar(value=True)

    def create_component(
        text: str, variable: tk.BooleanVar
    ) -> tuple[tk.Frame, tk.Checkbutton, ttk.Progressbar, tk.Label]:
        row = tk.Frame(component_box, background=default_background, padx=8, pady=7)
        row.pack(fill="x", pady=2)
        header = tk.Frame(row, background=default_background)
        header.pack(fill="x")
        check = tk.Checkbutton(
            header,
            text=text,
            variable=variable,
            background=default_background,
            activebackground=default_background,
            anchor="w",
        )
        check.pack(side="left")
        state_label = tk.Label(header, text="", background=default_background)
        state_label.pack(side="right")
        bar = ttk.Progressbar(row, maximum=100, mode="determinate")
        return row, check, bar, state_label

    ff_row, ff_check, ff_bar, ff_state = create_component(
        "FFmpeg 및 FFprobe", install_ffmpeg
    )
    yt_row, yt_check, yt_bar, yt_state = create_component("yt-dlp", install_ytdlp)

    buttons = ttk.Frame(content)
    buttons.pack(side="bottom", fill="x")
    close_button = ttk.Button(buttons, text="닫기", command=root.destroy)
    close_button.pack(side="right")
    next_button = ttk.Button(buttons, text="다음")
    next_button.pack(side="right", padx=(0, 8))
    stop_button = ttk.Button(buttons, text="중지", state="disabled")

    events: queue.Queue[tuple[str, object]] = queue.Queue()
    stop_event = threading.Event()
    failed = False

    def set_highlight(
        row: tk.Frame,
        check: tk.Checkbutton,
        label: tk.Label,
        active: bool,
    ) -> None:
        background = active_background if active else default_background
        row.configure(background=background)
        header = check.master
        header.configure(background=background)
        check.configure(background=background, activebackground=background)
        label.configure(background=background)

    def report(message: str, total: float, current: float | None) -> None:
        events.put(("progress", (message, total, current)))

    def worker(selected_ytdlp: bool, selected_ffmpeg: bool) -> None:
        try:
            update(selected_ytdlp, selected_ffmpeg, report, stop_event.is_set)
        except UpdateCancelled:
            events.put(("cancelled", None))
        except Exception as exc:
            events.put(("error", str(exc)))
        else:
            events.put(("done", None))

    def finish_controls() -> None:
        stop_button.configure(state="disabled")
        close_button.configure(state="normal")
        root.protocol("WM_DELETE_WINDOW", root.destroy)

    def poll() -> None:
        nonlocal failed
        while True:
            try:
                event, payload = events.get_nowait()
            except queue.Empty:
                break
            if event == "progress":
                message, _total, current = payload
                message = str(message)
                if message.startswith("yt-dlp"):
                    active_component = "yt-dlp"
                    set_highlight(ff_row, ff_check, ff_state, False)
                    if install_ffmpeg.get():
                        ff_state.configure(text="완료")
                        ff_bar["value"] = 100
                    set_highlight(yt_row, yt_check, yt_state, True)
                    yt_state.configure(text=message)
                    if current is not None:
                        yt_bar["value"] = float(current)
                elif message.startswith("FFmpeg"):
                    active_component = "ffmpeg"
                    set_highlight(yt_row, yt_check, yt_state, False)
                    set_highlight(ff_row, ff_check, ff_state, True)
                    ff_state.configure(text=message)
                    if current is not None:
                        ff_bar["value"] = float(current)
                elif message.startswith("최신 구성 요소"):
                    if install_ytdlp.get():
                        yt_state.configure(text="완료")
                        yt_bar["value"] = 100
                    if install_ffmpeg.get():
                        ff_state.configure(text="완료")
                        ff_bar["value"] = 100
                    set_highlight(yt_row, yt_check, yt_state, False)
                    set_highlight(ff_row, ff_check, ff_state, False)
            elif event == "error":
                failed = True
                active_state = yt_state if active_component == "yt-dlp" else ff_state
                active_state.configure(text=f"실패: {payload}")
                finish_controls()
            elif event == "cancelled":
                if str(ff_state.cget("text")) and ff_state.cget("text") != "완료":
                    ff_state.configure(text="중지됨")
                elif str(yt_state.cget("text")) != "완료":
                    yt_state.configure(text="중지됨")
                finish_controls()
            elif event == "done":
                instruction_label.configure(text="작업을 완료했습니다.")
                finish_controls()
        if str(close_button["state"]) == "disabled":
            root.after(100, poll)

    def begin_install() -> None:
        if not install_ytdlp.get() and not install_ffmpeg.get():
            return
        yt_check.configure(state="disabled")
        ff_check.configure(state="disabled")
        instruction_label.configure(text="작업을 수행 중입니다.")
        next_button.pack_forget()
        close_button.configure(state="disabled")
        stop_button.pack(side="right", padx=(0, 8))
        stop_button.configure(state="normal")
        if install_ytdlp.get():
            yt_bar.pack(fill="x", pady=(7, 0))
        if install_ffmpeg.get():
            ff_bar.pack(fill="x", pady=(7, 0))
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        threading.Thread(
            target=worker,
            args=(install_ytdlp.get(), install_ffmpeg.get()),
            name="ave-binary-update",
            daemon=True,
        ).start()
        poll()

    def stop_install() -> None:
        stop_event.set()
        stop_button.configure(state="disabled")

    def update_next_state(*_args: object) -> None:
        next_button.configure(
            state="normal" if install_ytdlp.get() or install_ffmpeg.get() else "disabled"
        )

    install_ytdlp.trace_add("write", update_next_state)
    install_ffmpeg.trace_add("write", update_next_state)
    next_button.configure(command=begin_install)
    stop_button.configure(command=stop_install)

    root.update_idletasks()
    window_x = max(0, (root.winfo_screenwidth() - root.winfo_width()) // 2)
    window_y = max(0, (root.winfo_screenheight() - root.winfo_height()) // 2)
    root.geometry(f"+{window_x}+{window_y}")
    root.deiconify()
    root.mainloop()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
