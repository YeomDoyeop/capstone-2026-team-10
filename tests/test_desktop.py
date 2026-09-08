from pathlib import Path


def test_tray_icon_uses_the_same_play_favicon_colors():
    source = (Path(__file__).resolve().parents[1] / "app" / "desktop.py").read_text(
        encoding="utf-8"
    )

    assert "rounded_rectangle" in source
    assert "draw.polygon" in source
    assert '"#000000"' in source
    assert '"#f97316"' in source


def test_client_logs_use_an_independent_gui_instead_of_cmd():
    source = (Path(__file__).resolve().parents[1] / "app" / "desktop.py").read_text(
        encoding="utf-8"
    )

    assert 'root.title("AVE 클라이언트 로그")' in source
    assert 'pystray.MenuItem("로그 열기"' in source
    assert 'pystray.MenuItem("AVE 클라이언트", lambda: None, enabled=False)' in source
    assert source.count("pystray.Menu.SEPARATOR") == 2
    assert 'root.protocol("WM_DELETE_WINDOW", root.withdraw)' in source
    assert "icon.run_detached()" in source
    assert "LOG_WINDOW.run()" in source
    assert "subprocess.Popen" not in source
    assert '["cmd"' not in source
    assert "class GuiLogHandler(logging.Handler)" in source
    assert "self.log_handler.subscribe()" in source
    assert 'self.log_path.open("rb")' not in source
    assert 'text="로컬 API와 백그라운드 작업의 최근 실행 기록"' not in source
    assert 'if "vista" in style.theme_names()' in source
    assert 'style.theme_use("vista")' in source
    assert 'background="SystemWindow"' in source
    assert 'selectbackground="SystemHighlight"' in source
    assert 'style="Log.Vertical.TScrollbar"' not in source
    assert 'style="Log.Horizontal.TScrollbar"' not in source
    assert "실시간 로그 연결됨" not in source
    assert 'root.geometry("780x510")' in source
    assert "vertical.grid_remove()" in source
    assert "horizontal.grid_remove()" in source
    assert 'display_content = content.rstrip("\\r\\n")' in source
    assert 'text.insert("end-1c", display_content)' in source
    assert 'text.see("end")' in source
    assert "text.xview_moveto(0.0)" in source
    assert "root.iconphoto(True, window_icon)" in source
    assert "MessageBoxW" in source
    assert "MB_SETFOREGROUND | MB_TOPMOST" in source
    assert '0, message, "AVE 클라이언트 종료", 0x50134' in source
    assert "parent=root" not in source
    assert 'default="no"' in source
    assert "_keep_taskbar_icon_only" not in source
    assert source.index("root.withdraw()") < source.index(
        'root.title("AVE 클라이언트 로그")'
    )
    assert 'root.configure(background="SystemButtonFace")' in source
    assert "tk.Toplevel(root)" not in source
