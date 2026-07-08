import argparse
import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler


VERSION = "1.2.1"


@dataclass(frozen=True)
class WatchdogConfig:
    # 轮询间隔（秒）
    interval_sec: float
    # explorer.exe 不存在时，是否尝试使用 SFC 修复（可能需要管理员权限，且耗时较长）
    try_repair_if_missing: bool
    # 只执行一次检查并退出（便于调试）
    once: bool
    # 日志文件路径（为空表示只输出到控制台）
    log_file: str | None
    # 是否启用托盘图标
    tray: bool
    # 状态文件路径（用于确认是否在运行）
    status_file: str | None


@dataclass
class WatchdogStatus:
    # 最近一次检查时间
    last_check_ts: float = 0.0
    # 最近一次检查时 explorer 是否存在/运行
    explorer_file_present: bool = True
    explorer_running: bool = True
    # 拉起次数
    restart_count: int = 0
    # 最近一次动作描述
    last_action: str = ""


def get_current_session_id() -> int:
    # 获取当前进程所在的 Windows Session ID，用于精确判断本会话的 explorer.exe 是否在运行
    session_id = ctypes.c_uint()
    ok = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id))
    if not ok:
        raise RuntimeError("无法获取当前 Session ID")
    return int(session_id.value)


def is_explorer_file_present(explorer_path: str) -> bool:
    # 判断 explorer.exe 文件是否存在
    try:
        return os.path.isfile(explorer_path)
    except OSError:
        return False


def is_explorer_running_in_session(session_id: int) -> bool:
    # 通过 tasklist 精确筛选当前会话（避免其他用户会话里存在 explorer.exe 导致误判）
    cmd = [
        "tasklist",
        "/FI",
        "IMAGENAME eq explorer.exe",
        "/FI",
        f"SESSION eq {session_id}",
        "/FO",
        "CSV",
        "/NH",
    ]
    try:
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags |= subprocess.CREATE_NO_WINDOW
        startupinfo = None
        if hasattr(subprocess, "STARTUPINFO") and hasattr(subprocess, "STARTF_USESHOWWINDOW") and hasattr(subprocess, "SW_HIDE"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except Exception:
        return False

    output = (proc.stdout or "").strip()
    if not output:
        return False

    # tasklist 命中时会输出一行 CSV（可能有多行），未命中时会输出类似 “信息: 没有运行的任务匹配指定标准。”
    for line in output.splitlines():
        if "explorer.exe" in line.lower():
            return True
    return False


def start_explorer(explorer_path: str) -> bool:
    # 在当前用户会话里拉起 explorer.exe（桌面/任务栏由 explorer 提供）
    try:
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags |= subprocess.CREATE_NO_WINDOW

        subprocess.Popen(
            [explorer_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        return True
    except Exception:
        return False


def get_default_log_file() -> str:
    # 默认日志目录放在当前用户的 LocalAppData 下，避免写入需要管理员权限的位置
    base_dir = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base_dir, "ExplorerWatchdog", "watchdog.log")


def get_default_status_file() -> str:
    base_dir = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base_dir, "ExplorerWatchdog", "status.txt")


def init_logger(log_file: str | None) -> logging.Logger:
    # 同时输出到控制台和日志文件，便于排查为什么 explorer 会反复重启
    logger = logging.getLogger("ExplorerWatchdog")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    if sys.stdout is not None:
        stream_handler = logging.StreamHandler(stream=sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def safe_open_file(path: str) -> None:
    # 通过系统默认方式打开文件/目录
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except Exception:
        return


def format_status_for_tooltip(status: WatchdogStatus) -> str:
    # 托盘提示文本最多约 128 字符，尽量简短
    running_text = "OK" if status.explorer_running else "DOWN"
    return f"ExplorerWatchdog {VERSION} | Explorer: {running_text} | Restarts: {status.restart_count}"


def run_watchdog_loop(
    config: WatchdogConfig,
    logger: logging.Logger,
    session_id: int,
    explorer_path: str,
    stop_event: threading.Event,
    status: WatchdogStatus,
    status_lock: threading.Lock,
) -> int:
    # 持续监控循环（支持 stop_event，用于托盘退出）
    while True:
        if stop_event.is_set():
            return 0

        exists = is_explorer_file_present(explorer_path)
        running = False
        action = ""

        if not exists:
            logger.warning("检测到 explorer.exe 文件不存在（或无法访问）")
            action = "file_missing"
            if config.try_repair_if_missing:
                logger.info("尝试使用 SFC 修复 explorer.exe（可能耗时且需要管理员权限）")
                try:
                    try_repair_explorer(explorer_path)
                except Exception as e:
                    logger.warning("SFC 修复执行失败：%s", e)
        else:
            running = is_explorer_running_in_session(session_id)
            if not running:
                logger.warning("检测到本会话 explorer.exe 未运行，尝试拉起")
                ok = start_explorer(explorer_path)
                if ok:
                    logger.info("explorer.exe 已尝试启动")
                    action = "restart_explorer"
                    with status_lock:
                        status.restart_count += 1
                else:
                    logger.warning("explorer.exe 启动失败（可能权限不足或系统异常）")
                    action = "restart_failed"

                # 避免 explorer 处于崩溃循环时被过于频繁地拉起
                time.sleep(3.0)
            else:
                action = "ok"

        with status_lock:
            status.last_check_ts = time.time()
            status.explorer_file_present = exists
            status.explorer_running = running if exists else False
            status.last_action = action
            restart_count = status.restart_count
            last_check_ts = status.last_check_ts
            explorer_running = status.explorer_running

        if config.status_file:
            try:
                content = (
                    f"version={VERSION}\n"
                    f"session_id={session_id}\n"
                    f"last_check_ts={int(last_check_ts)}\n"
                    f"explorer_running={int(explorer_running)}\n"
                    f"restart_count={restart_count}\n"
                    f"last_action={action}\n"
                )
                with open(config.status_file, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception:
                pass

        if config.once:
            return 0

        time.sleep(max(0.2, float(config.interval_sec)))


class TrayApp:
    # 使用 Win32 API 直接创建托盘图标，不依赖第三方库
    def __init__(
        self,
        logger: logging.Logger,
        status: WatchdogStatus,
        status_lock: threading.Lock,
        stop_event: threading.Event,
        log_file: str,
    ) -> None:
        self._logger = logger
        self._status = status
        self._status_lock = status_lock
        self._stop_event = stop_event
        self._log_file = log_file

        self._hwnd = None
        self._nid = None
        self._wm_taskbar = ctypes.windll.user32.RegisterWindowMessageW("TaskbarCreated")
        self._callback_msg = 0x8000 + 1

        self._ID_EXIT = 1001
        self._ID_OPEN_LOG = 1002

    def _create_window(self) -> int:
        HWND = ctypes.c_void_p
        UINT = ctypes.c_uint
        WPARAM = ctypes.c_size_t
        LPARAM = ctypes.c_ssize_t
        LRESULT = ctypes.c_ssize_t

        WNDPROCTYPE = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

        def _wnd_proc(hwnd, msg, wparam, lparam):  # type: ignore[no-untyped-def]
            if msg == self._wm_taskbar:
                self._refresh_icon()
                return 0

            if msg == self._callback_msg:
                if lparam == 0x0203:  # WM_LBUTTONDBLCLK
                    self._show_balloon()
                elif lparam == 0x0205:  # WM_RBUTTONUP
                    self._show_menu()
                return 0

            if msg == 0x0111:  # WM_COMMAND
                cmd_id = int(wparam) & 0xFFFF
                if cmd_id == self._ID_EXIT:
                    self._stop_event.set()
                    ctypes.windll.user32.PostQuitMessage(0)
                elif cmd_id == self._ID_OPEN_LOG:
                    safe_open_file(self._log_file)
                return 0

            if msg == 0x0002:  # WM_DESTROY
                self._delete_icon()
                ctypes.windll.user32.PostQuitMessage(0)
                return 0

            return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wnd_proc = WNDPROCTYPE(_wnd_proc)  # type: ignore[attr-defined]

        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
            ]

        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        class_name = "ExplorerWatchdogTrayWindow"
        wc = WNDCLASS()
        wc.lpfnWndProc = ctypes.cast(self._wnd_proc, ctypes.c_void_p).value
        wc.hInstance = hinst
        wc.lpszClassName = class_name

        atom = ctypes.windll.user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            # 可能已经注册过
            pass

        hwnd = ctypes.windll.user32.CreateWindowExW(
            0,
            class_name,
            class_name,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            hinst,
            None,
        )
        if not hwnd:
            raise RuntimeError("创建托盘窗口失败")
        self._hwnd = hwnd
        return self._hwnd

    def _get_icon_handle(self) -> int:
        # 使用系统内置图标，避免额外资源文件
        IDI_INFORMATION = ctypes.c_wchar_p(32516)
        hicon = ctypes.windll.user32.LoadIconW(None, IDI_INFORMATION)
        return int(hicon) if hicon else 0

    def _add_icon(self) -> None:
        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hWnd", ctypes.c_void_p),
                ("uID", ctypes.c_uint),
                ("uFlags", ctypes.c_uint),
                ("uCallbackMessage", ctypes.c_uint),
                ("hIcon", ctypes.c_void_p),
                ("szTip", ctypes.c_wchar * 128),
                ("dwState", ctypes.c_uint),
                ("dwStateMask", ctypes.c_uint),
                ("szInfo", ctypes.c_wchar * 256),
                ("uTimeoutOrVersion", ctypes.c_uint),
                ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", ctypes.c_uint),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", ctypes.c_void_p),
            ]

        NIF_MESSAGE = 0x00000001
        NIF_ICON = 0x00000002
        NIF_TIP = 0x00000004
        NIM_ADD = 0x00000000

        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = ctypes.c_void_p(self._hwnd)
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = self._callback_msg
        nid.hIcon = ctypes.c_void_p(self._get_icon_handle())

        with self._status_lock:
            tip = format_status_for_tooltip(self._status)
        nid.szTip = tip[:127]

        ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._nid = nid

    def _modify_icon_tip(self) -> None:
        if self._nid is None:
            return

        NIF_TIP = 0x00000004
        NIM_MODIFY = 0x00000001
        self._nid.uFlags = NIF_TIP
        with self._status_lock:
            tip = format_status_for_tooltip(self._status)
        self._nid.szTip = tip[:127]
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def _delete_icon(self) -> None:
        if self._nid is None:
            return
        NIM_DELETE = 0x00000002
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        self._nid = None

    def _refresh_icon(self) -> None:
        self._delete_icon()
        self._add_icon()

    def _show_balloon(self) -> None:
        # 双击托盘图标时弹出当前状态
        if self._nid is None:
            return

        NIF_INFO = 0x00000010
        NIM_MODIFY = 0x00000001
        NIIF_INFO = 0x00000001

        self._nid.uFlags = NIF_INFO
        self._nid.dwInfoFlags = NIIF_INFO

        with self._status_lock:
            last_action = self._status.last_action
            restart_count = self._status.restart_count
            explorer_running = self._status.explorer_running

        title = "ExplorerWatchdog"
        info = f"Explorer running: {explorer_running}\nRestarts: {restart_count}\nLast action: {last_action}"
        self._nid.szInfoTitle = title[:63]
        self._nid.szInfo = info[:255]
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def _show_menu(self) -> None:
        # 右键菜单：打开日志 / 退出
        hmenu = ctypes.windll.user32.CreatePopupMenu()
        MF_STRING = 0x00000000
        ctypes.windll.user32.AppendMenuW(hmenu, MF_STRING, self._ID_OPEN_LOG, "打开日志")
        ctypes.windll.user32.AppendMenuW(hmenu, MF_STRING, self._ID_EXIT, "退出")

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        ctypes.windll.user32.SetForegroundWindow(self._hwnd)
        ctypes.windll.user32.TrackPopupMenu(hmenu, 0, pt.x, pt.y, 0, self._hwnd, None)
        ctypes.windll.user32.DestroyMenu(hmenu)

    def run(self) -> int:
        # 托盘主循环：创建窗口、添加图标、跑消息循环
        self._create_window()
        self._add_icon()

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint),
                ("pt", POINT),
            ]

        msg = MSG()
        while True:
            # 每轮空闲时刷新一次提示文本，避免频繁调用影响性能
            self._modify_icon_tip()
            for _ in range(10):
                if ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                    ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
                if self._stop_event.is_set():
                    self._delete_icon()
                    return 0
                time.sleep(0.2)


def acquire_single_instance_mutex(session_id: int) -> int | None:
    # 每个会话只允许启动一个 watchdog，防止重复启动导致轮询/拉起行为被放大
    name = f"Local\\ExplorerWatchdog_Session_{session_id}"
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None

    already_exists = ctypes.windll.kernel32.GetLastError() == 183
    if already_exists:
        ctypes.windll.kernel32.CloseHandle(handle)
        return None

    return int(handle)


def try_repair_explorer(explorer_path: str) -> None:
    # 尝试用 SFC 修复指定文件；该操作可能需要管理员权限且耗时较长
    # 注意：此步骤不会强制执行，只有在用户开启 try_repair_if_missing 时才会触发
    sfc_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "sfc.exe")
    if not os.path.isfile(sfc_path):
        return

    cmd = [sfc_path, f"/scanfile={explorer_path}"]
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW
    startupinfo = None
    if hasattr(subprocess, "STARTUPINFO") and hasattr(subprocess, "STARTF_USESHOWWINDOW") and hasattr(subprocess, "SW_HIDE"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )


def run_watchdog(config: WatchdogConfig) -> int:
    explorer_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "explorer.exe")

    try:
        session_id = get_current_session_id()
    except Exception as e:
        print(f"[watchdog] 获取 Session ID 失败：{e}")
        return 2

    log_file = config.log_file if config.log_file is not None else get_default_log_file()
    logger = init_logger(log_file)

    mutex_handle = acquire_single_instance_mutex(session_id)
    if mutex_handle is None:
        logger.info("当前会话已存在一个 watchdog 实例，本次直接退出")
        return 0

    logger.info("当前会话 Session ID: %s", session_id)
    logger.info("监控目标: %s", explorer_path)
    logger.info("轮询间隔: %ss", config.interval_sec)
    logger.info("日志文件: %s", log_file)
    logger.info("版本: %s", VERSION)

    stop_event = threading.Event()
    status = WatchdogStatus()
    status_lock = threading.Lock()

    if config.tray:
        # 托盘模式：监控在后台线程运行，主线程跑托盘消息循环
        try:
            worker = threading.Thread(
                target=run_watchdog_loop,
                args=(config, logger, session_id, explorer_path, stop_event, status, status_lock),
                daemon=True,
            )
            worker.start()

            tray = TrayApp(logger=logger, status=status, status_lock=status_lock, stop_event=stop_event, log_file=log_file)
            return tray.run()
        except Exception as e:
            logger.warning("托盘启动失败，将以无托盘模式继续运行：%s", e)
            logger.warning(traceback.format_exc())
            return run_watchdog_loop(config, logger, session_id, explorer_path, stop_event, status, status_lock)

    return run_watchdog_loop(config, logger, session_id, explorer_path, stop_event, status, status_lock)


def parse_args(argv: list[str]) -> WatchdogConfig:
    # 参数解析放在 __main__ 下调用，保证默认参数可直接运行调试
    parser = argparse.ArgumentParser(description="监控当前会话的 explorer.exe，并在异常退出时自动拉起")
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="轮询间隔（秒），默认 5.0",
    )
    parser.add_argument(
        "--try-repair-if-missing",
        action="store_true",
        help="当 explorer.exe 文件不存在时，尝试用 SFC 扫描修复（可能需要管理员权限）",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default="",
        help="日志文件路径（默认写入 LocalAppData\\ExplorerWatchdog\\watchdog.log）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只执行一次检查并退出（用于验证脚本是否工作）",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="启用托盘图标（推荐后台运行时使用）",
    )
    parser.add_argument(
        "--status-file",
        type=str,
        default="",
        help="状态文件路径（默认写入 LocalAppData\\ExplorerWatchdog\\status.txt）",
    )
    ns = parser.parse_args(argv)
    return WatchdogConfig(
        interval_sec=ns.interval,
        try_repair_if_missing=ns.try_repair_if_missing,
        once=ns.once,
        log_file=(ns.log_file if ns.log_file else None),
        tray=ns.tray,
        status_file=(ns.status_file if ns.status_file else get_default_status_file()),
    )


if __name__ == "__main__":
    cfg = parse_args(sys.argv[1:])
    raise SystemExit(run_watchdog(cfg))
