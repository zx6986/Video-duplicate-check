# -*- coding: utf-8 -*-
"""
重复文件 / 同名文件查找工具
=====================================
功能：
  1. 选择一个文件夹（可选是否包含子文件夹）
  2. 查找「内容完全相同的重复文件」和/或「文件名相同的文件」
  3. 可选「相似视频」检测：抽帧算感知哈希，找出同一视频的不同版本
     （例如 1080p 与 720p、不同码率、改过名字的同一个视频）
  4. 结果按文件大小排列（组间、组内都按大小排序）
  5. 每个文件都标出完整路径和文件大小（可读大小 + 字节数）
  6. 可导出 CSV / TXT 报告，可双击打开文件或所在文件夹

运行环境：
  - 查重 / 查同名：Python 3.8+ 标准库即可
  - 相似视频检测：需要 OpenCV（import cv2，含 img_hash），没有则该项自动禁用
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import queue
import re
import shutil
import stat as stat_mod
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.platform == "win32":  # 让窗口在高分屏上不发虚
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "重复文件 / 同名文件查找工具"
CHUNK_SIZE = 4 * 1024 * 1024        # 哈希读取块加大到 4MB：大文件少 4 倍系统调用，磁盘更友好
HEAD_LIMIT = 128 * 1024             # 第一轮只读文件头 128KB 做快速比对
UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


class ScanAborted(Exception):
    """用户点了「停止」：用它从深层的哈希 / 解码循环里立刻跳出来"""


def lower_thread_priority() -> bool:
    """把当前线程优先级降一级（Windows），扫描时不跟前台程序抢 CPU。

    用线程级而不是进程级：界面线程保持正常优先级，窗口依旧流畅，
    只有后台扫描/解码线程让路。失败不影响功能。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # 必须显式声明：GetCurrentThread 返回的伪句柄在 64 位下是 -2，
        # 若按默认 c_int 传递会被截断，SetThreadPriority 会拿到无效句柄。
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        kernel32.SetThreadPriority.restype = ctypes.c_int
        handle = kernel32.GetCurrentThread()
        return bool(kernel32.SetThreadPriority(handle, -1))  # THREAD_PRIORITY_BELOW_NORMAL
    except Exception:
        return False


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def human_size(num: int) -> str:
    """把字节数变成易读的大小，例如 12.34 MB"""
    if num < 1024:
        return f"{num} B"
    value = float(num)
    idx = -1
    while value >= 1024 and idx < len(UNITS) - 2:
        value /= 1024.0
        idx += 1
    return f"{value:.2f} {UNITS[idx + 1]}"


def read_hash(path: str, limit: int | None = None,
              stop: threading.Event | None = None) -> str:
    """计算文件 BLAKE2b 摘要（比 SHA1 快 2~3 倍，结果同样稳定）。
    limit 不为 None 时只读前面 limit 字节；stop 置位时抛 ScanAborted，
    让几十 GB 的大文件也能中途停下，不必等它读完。"""
    # digest_size=20 与 SHA1 同为 160 位，碰撞概率可忽略，但速度快得多
    h = hashlib.blake2b(digest_size=20)
    read = 0
    with open(path, "rb") as fh:
        while True:
            if stop is not None and stop.is_set():
                raise ScanAborted()
            if limit is None:
                n = CHUNK_SIZE
            else:
                n = min(CHUNK_SIZE, limit - read)
                if n <= 0:
                    break
            block = fh.read(n)
            if not block:
                break
            h.update(block)
            read += len(block)
    return h.hexdigest()


def name_key(name: str, ignore_ext: bool) -> str:
    """同名比较用的键；Windows 下文件名不区分大小写"""
    if ignore_ext:
        name = os.path.splitext(name)[0]
    return name.casefold()


def reveal_in_explorer(path: str) -> None:
    """在资源管理器中打开所在文件夹并选中该文件（macOS / Linux 用对应命令）"""
    path = os.path.normpath(path)
    if not os.path.exists(path):
        messagebox.showwarning(APP_TITLE, f"文件已不存在，可能刚被删除或移动：\n{path}")
        return
    try:
        if sys.platform == "win32":
            # 坑：explorer.exe 不认带引号的参数。Python 列表形式的 Popen 会自动把
            # 含空格的参数加引号 → explorer 解析失败，打开的是错误目录。
            # 必须手工拼未加引号的裸命令行，并设 CREATION_FLAGS=0 跳过自动转义
            # （subprocess.list2cmdline 会转义反斜杠，同样不能走列表形式）。
            cmd = f'explorer.exe /select,"{path}"' if " " in path \
                else f'explorer.exe /select,{path}'
            subprocess.Popen(cmd, creationflags=0)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])
    except Exception:
        pass


def open_file(path: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        messagebox.showerror(APP_TITLE, f"无法打开文件：\n{path}\n\n{exc}")


# --------------------------------------------------------------------------
# 相似视频检测：抽帧 + 感知哈希（pHash）
#   逐字节查重抓不到「1080p 与 720p」这类同一内容的不同版本，因为它们每次
#   转码后字节完全不同。这里改用感知哈希：对画面做 DCT 取低频，分辨率、
#   码率、轻微压缩都不影响结果，再按时间顺序比对汉明距离。
# --------------------------------------------------------------------------
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".m2ts", ".mts",
    ".webm", ".mpg", ".mpeg", ".rmvb", ".rm", ".3gp", ".vob", ".f4v", ".asf", ".ogv",
}
DEFAULT_FRAMES = 12          # 每个视频抽多少帧
DEFAULT_DIST = 10.0          # 平均汉明距离上限（0~64，越小越严格）
DEFAULT_DURATION_TOL = 0.10  # 时长容差，超过就不必比对
DEFAULT_MIN_SECONDS = 3.0    # 跳过太短的片段
CACHE_VERSION = 4            # 抽帧/哈希方式变更时递增，旧缓存自动作废
# v4：密集抽帧改走 ffmpeg 顺序解码管线（哈希与 v3 的 cv2 seek 管线不通用，
#     缓存键也按管线分开，避免混用导致漏检）。

# ffmpeg 解码管线（可选）：存在 ffmpeg.exe 时，抽帧改走「顺序解码+按间隔抽帧」。
# 实测（90s 1080p，每 2 秒抽 1 帧）：cv2 逐帧 seek ≈2.9s 且全程占满 CPU；
# ffmpeg 顺序解码 ≈1.0s、真实 CPU 时间只要 0.47s，再以 IDLE 优先级运行——
# 既快几倍，又不跟前台程序抢 CPU，笔记本风扇也不会狂转。
# 注意：ffmpeg 管线与 cv2 seek 管线抽出的帧位置/缩放不同，pHash 不通用，
# 缓存键必须按管线分开（见 ff_pipeline_tag），否则会漏检。

def find_ffmpeg() -> str | None:
    """定位可用的 ffmpeg.exe：先看工具自身目录（含其下的 ffmpeg 文件夹），
    再找 PATH，再看常见安装位置，最后找已安装的 imageio-ffmpeg。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in ("ffmpeg.exe", os.path.join("ffmpeg", "ffmpeg.exe"),
                 "ffmpeg-win-x86_64-v7.1.exe", "ffmpeg-win64.exe"):
        local = os.path.join(here, cand)
        if os.path.isfile(local):
            return local
    found = shutil.which("ffmpeg")
    if found:
        return found
    # 常见的安装位置：winget/chocolatey/scoop、Gyan FFmpeg、以及 imageio-ffmpeg 的缓存
    for cand in _FFMPEG_KNOWN_DIRS:
        if os.path.isfile(cand):
            return cand
    try:
        import imageio_ffmpeg  # type: ignore
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return None


def _ffmpeg_known_dirs() -> list:
    """Windows 上几个最常见的 ffmpeg.exe 落点（都是拼路径，不做目录扫描，够快）。"""
    out = []
    local = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("PROGRAMFILES", "")
    if local:
        out.append(os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
        out.append(os.path.join(local, "imageio_ffmpeg", "binaries"))
    if program_files:
        out.append(os.path.join(program_files, "ffmpeg", "bin", "ffmpeg.exe"))
    home = os.path.expanduser("~")
    for d in (".", r"ffmpeg-master-latest-win64-gpl\bin", r"scoop\shims",
              r"AppData\Roaming\Python"):
        out.append(os.path.join(home, d))
    exe_paths = [p for p in out if p.endswith(os.sep) or os.path.isdir(p)]
    result = []
    for p in out:
        if os.path.isdir(p):
            try:
                for name in os.listdir(p):
                    if name.lower().startswith("ffmpeg") and name.lower().endswith(".exe"):
                        result.append(os.path.join(p, name))
            except OSError:
                pass
        elif os.path.isfile(p):
            result.append(p)
    return result


_FFMPEG_KNOWN_DIRS = _ffmpeg_known_dirs()


def _creationflags_idle() -> int:
    """Windows：ffmpeg 子进程的启动标志 = IDLE 优先级 + 不分配控制台窗口。

    IDLE_PRIORITY_CLASS：只在 CPU 空闲时干活，前台程序（游戏/浏览器/Office）
    永远优先，扫描不再拖慢整机。
    CREATE_NO_WINDOW：工具用 pythonw.exe 静默启动，自身没有控制台——不显式
    隐藏的话，每个 ffmpeg 子进程都会新开一个黑色控制台窗口（3 个解码线程
    = 3 个「弹窗」，扫描结束才消失）。"""
    if sys.platform == "win32":
        return subprocess.IDLE_PRIORITY_CLASS | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def ff_extract_hashes(exe: str, path: str, interval: float, hash_size: int,
                      hw: str | None, stop=None, max_frames: int = 6000):
    """ffmpeg 顺序解码 + 按固定间隔抽帧 + 缩到 hash_size 灰度，逐帧算 pHash。

    返回 (hashes, stamps)；hashes 是 16 位十六进制字符串列表，stamps 是每帧秒数。
    - 只回传 hash_size×hash_size 的小帧（45 帧才 46KB），管道搬运开销可忽略；
    - `-threads 2` + IDLE 优先级：限死单进程最多吃两个核，且整进程让路给前台程序
      （实测 threads 不限速会吃满多核；1 又太慢，2 是墙钟/CPU 的平衡点）；
    - hw 非 None 时加 `-hwaccel` 走显卡解码（解码搬到 GPU，CPU 时间几乎为零；
      实测同一管线内 GPU 与 CPU 解出的帧 pHash 差异 ≤1 bit——GPU 缩放器的取整
      和软解不同，但阈值是 10/64，这个量级不影响分组，缓存可共用）；
    - 解码失败/0 帧时返回空列表，由调用方决定回退到 cv2。
    """
    import numpy as np  # cv2 的依赖，必随 opencv-python 存在
    cv2 = load_cv2()
    hasher = cv2.img_hash.PHash_create()
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-an", "-threads", "2"]
    if hw:
        cmd += ["-hwaccel", hw]
    cmd += ["-i", path,
            "-vf", f"fps=1/{interval:.6g},scale={hash_size}:{hash_size}:flags=bilinear,format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    hashes: list[str] = []
    stamps: list[float] = []
    frame_bytes = hash_size * hash_size
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         creationflags=_creationflags_idle())
    assert p.stdout is not None
    try:
        buf = bytearray()
        idx = 0
        while len(hashes) < max_frames:
            if stop is not None and stop.is_set():
                raise ScanAborted()
            chunk = p.stdout.read(frame_bytes)     # 流式：解码一帧就能读到一帧
            if not chunk:
                break
            buf += chunk
            while len(buf) >= frame_bytes:
                frame = np.frombuffer(bytes(buf[:frame_bytes]), dtype=np.uint8)
                del buf[:frame_bytes]
                frame = frame.reshape(hash_size, hash_size)
                hashes.append(f"{int.from_bytes(hasher.compute(frame).tobytes(), 'big'):016x}")
                stamps.append(round(idx * interval, 3))
                idx += 1
    finally:
        if p.poll() is None:
            p.kill()          # 停止/出错：立刻掐掉解码进程
        p.wait(timeout=5)
        p.stdout.close()
    return hashes, stamps


def load_cv2():
    """导入 OpenCV；没有则返回 None（其余功能不受影响）"""
    try:
        import cv2  # type: ignore
        return cv2
    except Exception:
        return None


def popcount(value: int) -> int:
    try:
        return value.bit_count()            # Python 3.10+
    except AttributeError:
        return bin(value).count("1")


def mmss(seconds: float) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def video_cache_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "similar_video_cache.json")


def video_cache_key(path: str, pipeline: str = ""):
    """缓存键：路径 + 大小 + 修改时间（+ 管线），文件一变缓存自动失效。

    pipeline：抽帧管线标签。cv2 seek 与 ffmpeg 顺序解码取到的帧位置/缩放不同，
    算出的 pHash 不通用，混用会漏检，所以按键分开存。
    Windows 文件名不允许出现“|”，所以用“|”作分隔符是安全的（键最多 4 段）。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    base = f"{path}|{st.st_size}|{int(st.st_mtime)}"
    return f"{base}|{pipeline}" if pipeline else base


def load_video_cache(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_video_cache(path: str, cache: dict) -> None:
    try:
        # 键格式 path|size|mtime[|pipeline]：从右往左剥掉固定字段就是路径
        alive = {k: v for k, v in cache.items() if os.path.exists(k.rsplit("|", 3)[0])}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(alive, fh, ensure_ascii=False)
    except Exception:
        pass


def _cache_hit(cache: dict | None, key):
    """缓存命中检查：键存在、有哈希、且是当前算法版本才复用。"""
    if cache is None or not key or key not in cache:
        return None
    entry = cache[key]
    if entry.get("hash") and entry.get("v") == CACHE_VERSION:
        return entry
    return None


def probe_video(cv2, path: str, frame_count: int, cache: dict | None = None,
                hash_size: int = 32, stop: threading.Event | None = None):
    """整片模式抽帧并算 pHash，返回 {"dur","w","h","fps","hash":[16 位十六进制, ...]}；失败返回 None

    hash_size：pHash 内部计算前先把帧缩到这个尺寸（默认 32）。这是纯 CPU 的降采样，
    占用极小，但能让 OpenCV 解码器用更轻的 scaler，降低解码时的 CPU 占用。
    stop：置位时抛 ScanAborted，让「停止」在解码途中就能生效。

    这里只抽 frame_count 帧（默认 12），实测 CPU 时间与片长基本无关（90s 1080p 约 0.3s），
    犯不上起 ffmpeg 子进程；密集/合集模式才用 ffmpeg 顺序解码（见 probe_video_dense）。
    缓存键带帧数：换了抽帧数量不能复用旧指纹。
    """
    key = video_cache_key(path, f"uni:{frame_count}")
    if key is None:
        return None

    entry = _cache_hit(cache, key)
    if entry is not None:
        return entry                           # 命中当前版本的缓存，直接复用

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        cap.release()
        return None
    try:
        if hasattr(cv2, "CAP_PROP_ORIENTATION_AUTO"):
            try:
                cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)   # 手机竖拍视频自动转正
            except Exception:
                pass
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        duration = total / fps if (fps > 0 and total > 0) else 0.0

        # 拿到真实分辨率后，让解码器直接输出小图（hash_size×hash_size）。
        # pHash 反正只算 32x32，全尺寸帧的搬运和内部缩放全省了；
        # 只在 FFmpeg 后端启用（MSMF 等后端可能不兼容，保持原样）
        if width > 0 and height > 0 and hash_size > 0:
            try:
                backend = cap.get(cv2.CAP_PROP_BACKEND) if hasattr(cv2, "CAP_PROP_BACKEND") else -1
                if backend in (-1, cv2.CAP_FFMPEG):
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, hash_size)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, hash_size)
            except Exception:
                pass
        hasher = cv2.img_hash.PHash_create()
        hashes: list[int] = []

        if total > 1 and fps > 0:
            # 常规：按比例定位，均匀抽 frame_count 帧
            for i in range(frame_count):
                if stop is not None and stop.is_set():
                    raise ScanAborted()
                idx = max(0, min(int((i + 0.5) * total / frame_count), total - 1))
                if not cap.set(cv2.CAP_PROP_POS_FRAMES, idx):
                    continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                if not width or not height:
                    height, width = frame.shape[:2]
                hashes.append(int.from_bytes(hasher.compute(frame).tobytes(), "big"))
        else:
            # 元数据不全（个别流媒体/损坏文件）：顺读一段再均匀取样
            raw: list[int] = []
            for _ in range(4000):
                if stop is not None and stop.is_set():
                    raise ScanAborted()
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                if not width or not height:
                    height, width = frame.shape[:2]
                raw.append(int.from_bytes(hasher.compute(frame).tobytes(), "big"))
            if len(raw) > frame_count:
                picks = [min(int((i + 0.5) * len(raw) / frame_count), len(raw) - 1)
                         for i in range(frame_count)]
                hashes = [raw[k] for k in picks]
            else:
                hashes = raw
            duration = 0.0

        entry = {
            "dur": round(duration, 3), "w": width, "h": height,
            "fps": round(fps, 2), "hash": [f"{h:016x}" for h in hashes],
            "v": CACHE_VERSION,
        }
        if cache is not None and key:
            cache[key] = entry
        return entry
    except ScanAborted:
        raise
    except Exception:
        return None
    finally:
        cap.release()


def hash_distances(hashes_a: list, hashes_b: list):
    """按时间顺序逐帧比汉明距离；两个序列长度差太多时先重采样对齐"""
    if abs(len(hashes_a) - len(hashes_b)) > 2 and hashes_a and hashes_b:
        n = min(len(hashes_a), len(hashes_b))
        hashes_a = [hashes_a[int(i * len(hashes_a) / n)] for i in range(n)]
        hashes_b = [hashes_b[int(i * len(hashes_b) / n)] for i in range(n)]
    n = min(len(hashes_a), len(hashes_b))
    if n < 2:
        return None
    return [popcount(int(hashes_a[i], 16) ^ int(hashes_b[i], 16)) for i in range(n)]


# --------------------------------------------------------------------------
# 密集抽帧（固定间隔）+ 序列包含比对：解决「合集包含了别的视频」
# --------------------------------------------------------------------------
FFMPEG_MIN_BYTES = 25 * 1024 * 1024   # 实测拐点：小文件 cv2 seek 只要 17ms，ffmpeg 起进程要 158ms
FFMPEG_ANY_BYTES = 4 * 1024 * 1024    # 「纯 CPU 顺序解码」模式下更激进地走 ffmpeg

# 界面上的「逐帧解码」选项 → 内部代码（见 wants_gpu / route_bytes_for）
# 实测（32 核 + RTX 4060 笔记本，4 个 36~488MB 视频整批逐帧扫描）：
#   OpenCV 逐帧 seek：8.6s 墙钟，平均吃 25.5% 全部核心，峰值 87.8%  ← 会满载
#   ffmpeg 顺序解码：7~19s 墙钟，平均 0.3%，峰值 2.9%              ← 完全不抢前台
DECODE_MODES = [
    ("自动（推荐）：大文件顺序解码，HEVC/AV1/VP9 走显卡", "auto"),
    ("更省 CPU：全部走显卡解码（重码流明显，普通 H264 会更慢）", "gpu"),
    ("更快：只用 CPU 顺序解码（小文件也提前走顺序解码）", "cpu"),
]


def ff_read_meta(cv2, path: str):
    """只用 cv2 打开容器读元信息（不解码画面，约十几毫秒），失败返回 None。

    额外读 FOURCC（编码名）：自动模式靠它判断这个文件值不值得交给显卡解码。"""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        cap.release()
        return None
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        duration = total / fps if (fps > 0 and total > 0) else 0.0
        if duration <= 0:
            return None
        try:
            fv = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
            codec = "".join(chr((fv >> (8 * i)) & 0x7F) for i in range(4)).strip().lower()
        except Exception:
            codec = ""
        return {"dur": round(duration, 3), "w": width, "h": height,
                "fps": round(fps, 2), "codec": codec}
    except Exception:
        return None
    finally:
        cap.release()


HEAVY_CODECS = {"hevc", "h265", "av01", "av1", "vp09", "vp9"}


def wants_gpu(mode: str, meta: dict) -> bool:
    """决定 ffmpeg 是否加 -hwaccel cuda（显卡解码）。

    实测（RTX 4060 笔记本，每 2 秒抽 1 帧）真实 CPU 时间：
      HEVC 1080p 90s：CPU 解码 625~844ms → cuda 78~110ms（省 8 成，墙钟还快 30%）
      H264 1080p 90s：CPU 解码 62~469ms 已很低 → cuda 125~828ms、墙钟 2.6→7.0s，
                      起 GPU 上下文是净亏。
      H264 4K 60s   ：CPU 解码 6.2s 墙钟 / cuda 14.4s 墙钟 —— 4K 走显卡反而慢
                      （本机 GPU 还要伺候前台），所以自动模式不看分辨率。
    所以「自动」= 只在 CPU 软解确实吃不消的重码流（HEVC/AV1/VP9）用显卡；
    「更省 CPU：显卡解码」= 一律用（把解码 CPU 全部转给 GPU，代价是墙钟更慢）；
    「更快：小文件也顺序解码」= 一律不用显卡。
    """
    if mode == "cpu":
        return False
    if mode == "gpu":
        return True
    codec = (meta.get("codec") or "").lower()
    return any(codec.startswith(p) for p in HEAVY_CODECS)


def route_bytes_for(mode: str) -> int:
    """多大开始值得走 ffmpeg 顺序解码（整批按中位数路由，见 scan_similar_videos）。

    小文件起 ffmpeg 子进程的固定开销（约 150ms 墙钟）不划算，用 cv2 就地 seek 更快；
    大文件才轮到 ffmpeg——一次顺序解码省掉 cv2 逐帧 seek 反复解 GOP 的开销。
    「更快：只用 CPU 顺序解码」没有 GPU 上下文开销，阈值放低（≥4MB），
    让中小文件也提前进入低 CPU 的顺序解码管线；
    「更省 CPU：全部走显卡解码」= 0，一个不落全交给 GPU。"""
    if mode == "gpu":
        return 0
    if mode == "cpu":
        return FFMPEG_ANY_BYTES
    return FFMPEG_MIN_BYTES


def _via_ffmpeg(cv2, ffexe, path, meta, mode, hash_size, stop,
                interval, max_frames=2000):
    """ffmpeg 顺序解码抽帧 → 密集指纹 entry；失败返回 None（调用方回退 cv2）。

    显卡解码失败（无独显/驱动忙/解码器不支持）→ 自动改用 CPU 解码重试一次，
    再失败才返回 None，保证不会因显卡问题漏扫。
    """
    hw = "cuda" if wants_gpu(mode, meta) else None
    dur = meta["dur"]
    # 与 cv2 管线一致的自适应间隔：把超长视频的帧数控制在 max_frames 以内
    eff = max(interval, dur / max_frames) if max_frames > 0 else interval
    for try_hw in ([hw, None] if hw else [None]):
        try:
            hashes, stamps = ff_extract_hashes(ffexe, path, eff, hash_size, try_hw,
                                               stop, max_frames=max_frames)
        except ScanAborted:
            raise
        except Exception:
            continue
        if len(hashes) >= 2:
            entry = dict(meta)
            entry.update({"hash": hashes, "ts": stamps,
                          "interval": round(eff, 3), "v": CACHE_VERSION})
            return entry
    return None


def probe_video_dense(cv2, path: str, interval: float = 2.0,
                      cache: dict | None = None, max_frames: int = 2000,
                      hash_size: int = 32, stop: threading.Event | None = None,
                      ffexe: str | None = None, mode: str = "auto"):
    """按固定时间间隔（默认每 2 秒）抽帧并算 pHash，返回带时间戳的指纹序列。

    两条解码管线，由调用方整批选定（传入 ffexe 即走 ffmpeg）：
      ffmpeg 顺序解码：一次顺序解完整片、按 fps 滤镜取帧。大文件显著更省 CPU
                       （90s 1080p：ffmpeg ≈0.5s CPU vs cv2 逐帧 seek ≈1.6~3.1s，
                       因为每次 seek 都要重复解码整个 GOP——这正是 CPU 满载的根因）。
                       mode 决定要不要显卡解码（cuda，pHash 与 CPU 解码逐位一致）。
      cv2 seek：        小文件更快（不必起子进程），大文件 CPU 高。
    两条管线取帧位置/缩放不同 → cv2 与 ffmpeg 的 pHash 不通用，缓存键带管线标签 + 间隔；
    ffmpeg 内部显卡解码与 CPU 解码的差异只有 ≤1 bit（GPU 缩放器取整不同，阈值 10 远盖得住），
    共用 ff: 缓存不影响分组。

    stop：置位时抛 ScanAborted，让「停止」在长视频抽帧途中就能生效。
    返回 {"dur","w","h","fps","hash":[...],"ts":[...],"interval":...,"v":CACHE_VERSION}
    ts[i] 是 hash[i] 对应的片内秒数；失败返回 None。
    """
    tag = f"{interval:g}"
    if ffexe:
        key = video_cache_key(path, f"ff:{tag}")
        entry = _cache_hit(cache, key)
        if entry is not None:
            return entry
        meta = ff_read_meta(cv2, path)
        if meta is not None:
            entry = _via_ffmpeg(cv2, ffexe, path, meta, mode, hash_size, stop,
                                interval=interval, max_frames=max_frames)
            if entry is not None:
                if cache is not None and key:
                    cache[key] = entry
                return entry
        # ffmpeg 抽帧失败（解码器不支持/文件损坏/显卡忙）→ 回退 cv2，不中断整轮扫描

    key = video_cache_key(path, f"cv:{tag}")
    if key is None:
        return None
    entry = _cache_hit(cache, key)
    if entry is not None:
        return entry

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        cap.release()
        return None
    try:
        if hasattr(cv2, "CAP_PROP_ORIENTATION_AUTO"):
            try:
                cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
            except Exception:
                pass
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        duration = total / fps if (fps > 0 and total > 0) else 0.0
        if duration <= 0:
            return None

        # 间隔自适应：片太长时自动放大间隔，把帧数控制在 max_frames 以内，
        # 防止超长视频产生过多帧导致内存和比对时间失控。
        eff_interval = max(interval, duration / max_frames) if max_frames > 0 else interval
        if width > 0 and height > 0 and hash_size > 0:
            try:
                backend = cap.get(cv2.CAP_PROP_BACKEND) if hasattr(cv2, "CAP_PROP_BACKEND") else -1
                if backend in (-1, cv2.CAP_FFMPEG):
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, hash_size)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, hash_size)
            except Exception:
                pass

        hasher = cv2.img_hash.PHash_create()
        hashes: list[int] = []
        stamps: list[float] = []
        t = 0.0
        while t < duration:
            if stop is not None and stop.is_set():
                raise ScanAborted()
            if not cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0):
                break
            ok, frame = cap.read()
            if not ok or frame is None:
                t += eff_interval
                continue
            if not width or not height:
                height, width = frame.shape[:2]
            hashes.append(int.from_bytes(hasher.compute(frame).tobytes(), "big"))
            stamps.append(round(t, 3))
            t += eff_interval

        if len(hashes) < 2:
            return None
        entry = {
            "dur": round(duration, 3), "w": width, "h": height,
            "fps": round(fps, 2), "hash": [f"{h:016x}" for h in hashes],
            "ts": stamps, "interval": round(eff_interval, 3), "v": CACHE_VERSION,
        }
        if cache is not None and key:
            cache[key] = entry
        return entry
    except ScanAborted:
        raise
    except Exception:
        return None
    finally:
        cap.release()


def find_containment(short_entry: dict, long_entry: dict, threshold: float,
                     min_match_ratio: float = 0.85):
    """把短视频的指纹序列在长视频上滑动比对，找「合集包含」的位置。

    short_entry / long_entry：probe_video_dense 的结果（带 ts 时间戳）。
    返回 {"pos": 起始帧下标, "start": 起始秒, "coverage": 覆盖率, "mean": 平均距离}
    或 None（不构成包含关系）。

    判定：存在一段连续窗口，其平均汉明距离 ≤ threshold，且窗口覆盖短视频
    ≥ min_match_ratio 的指纹帧（默认 85%），就视为「长视频包含了短视频」。
    """
    sh, lh = short_entry.get("hash", []), long_entry.get("hash", [])
    st, lt = short_entry.get("ts", []), long_entry.get("ts", [])
    n, m = len(sh), len(lh)
    if n < 2 or m < 2 or n > m:
        return None

    # 逐起点滑动：短视频第 i 帧对齐长视频第 pos+i 帧，算整段平均距离
    best = None
    for pos in range(m - n + 1):
        dists = [popcount(int(sh[i], 16) ^ int(lh[pos + i], 16)) for i in range(n)]
        mean = sum(dists) / n
        if best is None or mean < best[1]:
            best = (pos, mean)
        # 提前退出：距离已经很理想就不必再找更好的起点
        if mean <= threshold * 0.3:
            break
    if best is None:
        return None
    pos, mean = best
    if mean > threshold:
        return None
    coverage = n / m if m else 0.0
    return {"pos": pos, "start": lt[pos] if pos < len(lt) else 0.0,
            "coverage": coverage, "mean": mean}


def scan_similar_videos(
    files,
    frame_count: int = DEFAULT_FRAMES,
    threshold: float = DEFAULT_DIST,
    duration_tol: float = DEFAULT_DURATION_TOL,
    min_seconds: float = DEFAULT_MIN_SECONDS,
    dense: bool = False,
    dense_interval: float = 2.0,
    status=None,
    stop: threading.Event | None = None,
    cache_path: str | None = None,
    decode_mode: str = "auto",
):
    """返回分组列表。

    dense=False（整片模式）：按片长比例均匀抽帧，整片对整片比对，
        抓「1080p/720p 换码率」这种同内容不同版本。
    dense=True（逐帧模式）：按固定间隔 dense_interval 抽帧得到时间轴指纹，
        既做整片比对，也做「短视频在长视频上滑动比对」的合集包含检测。

    decode_mode（仅逐帧/合集模式生效）："auto" 大文件走 ffmpeg 顺序解码、
        HEVC/AV1/VP9 重码流交给显卡解码；"gpu" 一律显卡解码（最省 CPU、最慢）；
        "cpu" 一律不用显卡（中小文件也走顺序解码，综合最快）。
        没找到 ffmpeg.exe 时全部退回 OpenCV 逐帧 seek（大文件会明显更占 CPU）。

    每组 dict：{"items":[{path,size,name,w,h,dur,frames,sim,relation}...],
                "dur":..., "avg_sim":..., "kind":"same"|"contains"|"mixed"}
    relation（每个文件相对组内的关系）："same"=同内容 / "contained"=被包含 /
    "container"=包含别人。
    """
    emit = status if status is not None else (lambda _m: None)
    cv2 = load_cv2()
    if cv2 is None:
        raise RuntimeError("未检测到 OpenCV（cv2），无法进行相似视频比对。"
                           "可执行 pip install opencv-python 后重试。")

    def stopped() -> bool:
        return stop is not None and stop.is_set()

    videos = [f for f in files
              if os.path.splitext(f[2])[1].lower() in VIDEO_EXTS and f[1] >= 1024]
    if not videos:
        return []

    cache_path = cache_path or video_cache_path()
    cache = load_video_cache(cache_path)
    cache_snapshot = set(cache)

    # ffmpeg 只在逐帧/合集模式参与（整片模式抽 12 帧用 cv2 seek 就够快）。
    # 关键约束：本批视频必须走同一条管线——两条管线取帧位置不同，pHash 不通用，
    # 混批会在严格阈值下漏检。所以整批一起路由，用「中位数大小」决定：
    #   中位数偏小 → 全部用 OpenCV 就地 seek（省掉每个文件 ~150ms 的起进程开销）；
    #   中位数够大 → 全部交给 ffmpeg 顺序解码（省掉逐帧 seek 反复解 GOP 的 CPU）。
    # 用中位数而不是平均值，避免「一个超大文件把一堆小文件也拖去起进程」。
    ffexe = find_ffmpeg() if dense else None
    ff_reason = ""
    if ffexe:
        sizes = sorted(f[1] for f in videos)
        if sizes[len(sizes) // 2] < route_bytes_for(decode_mode):
            ffexe = None
            ff_reason = "本批多为小文件，OpenCV 就地取帧更快"
    if dense:
        if ffexe:
            gpu_note = {"auto": "自动：HEVC/AV1/VP9 走显卡",
                        "gpu": "全部走显卡解码",
                        "cpu": "只用 CPU 解码"}[decode_mode]
            emit(f"正在逐帧抽帧分析 {len(videos):,} 个视频"
                 f"（每 {dense_interval:g} 秒 1 帧，ffmpeg 顺序解码 + {gpu_note}，"
                 f"低优先级不抢前台）…")
        else:
            why = ff_reason or "未找到 ffmpeg.exe，改用 OpenCV 逐帧定位"
            emit(f"正在逐帧抽帧分析 {len(videos):,} 个视频"
                 f"（每 {dense_interval:g} 秒 1 帧，{why}）…")
    else:
        emit(f"正在抽帧分析 {len(videos):,} 个视频（每个 {frame_count} 帧）…")
    entries: dict[str, tuple] = {}
    done = 0
    total = len(videos)

    cpu = os.cpu_count() or 2
    if dense and ffexe:
        # ffmpeg 子进程本身是 IDLE 优先级 + 限 2 线程，再开 3 路足够吃满空闲核心；
        # 开更多只会让切换开销变大，对速度没帮助。
        workers = min(3, cpu)
    else:
        workers = min(4, cpu) if total <= 300 else 2

    def expected_keys(path: str):
        """该文件本次可能用到的缓存键（只为统计「命中缓存」的数量，不影响结果）。"""
        if dense:
            yield video_cache_key(path, f"cv:{dense_interval:g}")
            if ffexe:
                yield video_cache_key(path, f"ff:{dense_interval:g}")
        else:
            yield video_cache_key(path, f"uni:{frame_count}")

    def probe(item):
        if dense:
            return probe_video_dense(cv2, item[0], interval=dense_interval,
                                     cache=cache, stop=stop, ffexe=ffexe,
                                     mode=decode_mode)
        return probe_video(cv2, item[0], frame_count, cache=cache, stop=stop)

    aborted = False
    with ThreadPoolExecutor(max_workers=workers,
                            initializer=lower_thread_priority) as pool:
        futures = {pool.submit(probe, item): item for item in videos}
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                entry = fut.result()
            except ScanAborted:
                entry, aborted = None, True
            except Exception:
                entry = None
            done += 1
            if entry and len(entry.get("hash", [])) >= 2:
                if not entry["dur"] or entry["dur"] >= min_seconds:
                    entries[item[0]] = (item, entry)
            # 关键：用户点了「停止」就立刻收工——取消还没开始的解码任务，
            # 正在解码的会在下一帧自己抛 ScanAborted，几百毫秒内就能退出。
            if aborted or stopped():
                for f in futures:
                    f.cancel()
                aborted = True
                break
            if done % 5 == 0 or done == total:
                emit(f"正在抽帧分析… {done:,}/{total:,}")

    # 已解码的指纹照常写缓存：下次再扫同一批视频不用从头解码
    save_video_cache(cache_path, cache)
    if aborted or stopped():
        emit(f"已停止抽帧（完成 {done:,}/{total:,}，已解码的指纹已存入缓存）")
        return []
    reused = sum(1 for item in videos
                 if any(k and k in cache_snapshot for k in expected_keys(item[0])))
    if reused:
        emit(f"抽帧完成（{reused:,} 个视频命中缓存，未重复解码）")
    if len(entries) < 2:
        return []

    items = sorted(entries.values(), key=lambda x: x[1]["dur"] or 0)
    known = [x for x in items if x[1]["dur"]]
    unknown = [x for x in items if not x[1]["dur"]]

    # 候选对分两类：
    #  same_pairs  时长接近（容差内）→ 整片对整片
    #  cont_pairs  一长一短（短 < 长 × (1-容差)）→ 仅 dense 模式做包含比对
    same_pairs, cont_pairs = [], []
    for i, a in enumerate(known):
        for b in known[i + 1:]:
            if b[1]["dur"] - a[1]["dur"] > max(duration_tol * b[1]["dur"], 1.0):
                break
            same_pairs.append((a, b))
    if dense:
        # 时长相差明显的一对，短的尝试在长的里面找位置
        for a in known:
            for b in known:
                if a is b:
                    continue
                da, db = a[1]["dur"], b[1]["dur"]
                if da >= db * (1.0 - duration_tol):   # a 不短于 b，跳过
                    continue
                # 长度差越大越可能是包含；限制上限避免无意义的全配对
                cont_pairs.append((a, b))
    for i, a in enumerate(unknown):
        same_pairs.extend((a, b) for b in known)
        same_pairs.extend((a, b) for b in unknown[i + 1:])

    emit(f"正在比对 {len(same_pairs) + len(cont_pairs):,} 组候选"
         f"（整片 {len(same_pairs):,}，包含 {len(cont_pairs):,}）…")
    parent = {k: k for k in entries}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    scores: dict[tuple, float] = {}          # 整片相似度（距离）
    contains: dict[tuple, dict] = {}         # (short_path, long_path) -> 包含详情

    for i, (a, b) in enumerate(same_pairs, 1):
        if stopped():
            return []
        pa, pb = a[0][0], b[0][0]
        dists = hash_distances(a[1]["hash"], b[1]["hash"])
        if not dists:
            continue
        mean = sum(dists) / len(dists)
        median = sorted(dists)[len(dists) // 2]
        if mean <= threshold and median <= threshold + 4:
            scores[(pa, pb)] = mean
            union(pa, pb)
        if i % 2000 == 0:
            emit(f"正在比对… {i:,}/{len(same_pairs) + len(cont_pairs):,}")

    for j, (short, long) in enumerate(cont_pairs, 1):
        if stopped():
            return []
        ps, pl = short[0][0], long[0][0]
        hit = find_containment(short[1], long[1], threshold)
        if hit:
            contains[(ps, pl)] = hit
            union(ps, pl)
        if j % 200 == 0:
            emit(f"正在比对包含… {j:,}/{len(cont_pairs):,}")

    groups: dict[str, list[str]] = defaultdict(list)
    for path in entries:
        groups[find(path)].append(path)

    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        rows = []
        for path in members:
            item, entry = entries[path]
            sims = []
            relation = "same"
            for other in members:
                if other == path:
                    continue
                dist = scores.get((path, other), scores.get((other, path)))
                if dist is not None:
                    sims.append(1.0 - dist / 64.0)
                # 包含关系：我是短的（被别人包含）/ 我是长的（包含别人）
                if (path, other) in contains:
                    relation = "contained"
                    sims.append(1.0 - contains[(path, other)]["mean"] / 64.0)
                elif (other, path) in contains:
                    if relation != "contained":
                        relation = "container"
                    sims.append(1.0 - contains[(other, path)]["mean"] / 64.0)
            rows.append({
                "path": path, "size": item[1], "name": item[2],
                "w": entry["w"], "h": entry["h"], "dur": entry["dur"],
                "frames": len(entry["hash"]),
                "sim": (sum(sims) / len(sims) * 100.0) if sims else None,
                "relation": relation,
            })
        rows.sort(key=lambda r: r["size"], reverse=True)
        # 组内成对相似度保留为“边”列表：删除某个文件后重算组统计只需筛边，
        # 不必重新解码抽帧（这就是删一个文件不用再“读取很久”的关键）
        edges: list[list] = []
        for i, m1 in enumerate(members):
            for m2 in members[i + 1:]:
                dist = scores.get((m1, m2), scores.get((m2, m1)))
                if dist is not None:
                    edges.append([m1, m2, 1.0 - dist / 64.0])
                elif (m1, m2) in contains:
                    edges.append([m1, m2, 1.0 - contains[(m1, m2)]["mean"] / 64.0])
                elif (m2, m1) in contains:
                    edges.append([m1, m2, 1.0 - contains[(m2, m1)]["mean"] / 64.0])
        edge_sims = [e[2] for e in edges]
        has_contains = any(r["relation"] != "same" for r in rows)
        out.append({
            "items": rows,
            "dur": max(r["dur"] for r in rows),
            "avg_sim": (sum(edge_sims) / len(edge_sims) * 100.0) if edge_sims else None,
            "kind": "contains" if has_contains else "same",
            "contains": {k: v for k, v in contains.items()
                         if k[0] in members or k[1] in members},
            "edges": edges,
        })
    out.sort(key=lambda g: max(r["size"] for r in g["items"]), reverse=True)
    return out


# --------------------------------------------------------------------------
# 扫描核心（与界面无关，方便单独测试）
# --------------------------------------------------------------------------
class ScanResult:
    def __init__(self) -> None:
        self.files: list[tuple[str, int, str]] = []          # (完整路径, 字节, 文件名)
        self.content_groups: list[list[tuple[str, int, str]]] = []
        self.name_groups: list[list[tuple[str, int, str]]] = []
        self.video_groups: list[dict] = []                    # 相似视频分组
        self.video_note = ""                                  # 相似视频相关的提示/错误
        self.hash_map: dict[str, str] = {}                    # 路径 -> 内容哈希
        self.errors: list[str] = []
        self.scanned = 0
        self.seconds = 0.0
        self.stopped = False

    @property
    def wasted_bytes(self) -> int:
        """内容重复文件里，除每组保留一份之外多出来的体积"""
        total = 0
        for group in self.content_groups:
            sizes = sorted((sz for _, sz, _ in group), reverse=True)
            total += sum(sizes[1:])
        return total


def prune_result(res: "ScanResult", removed: set) -> "ScanResult":
    """从已有结果里剔除指定文件，就地更新，不重新扫描。

    原理：删掉一个文件不会让剩下的文件之间产生任何新的重复/相似关系
    （任意一对的判定只取决于这一对自己），所以只需逐组筛掉成员，
    再用保留的「关系边」重算组统计即可——几十秒的抽帧完全不用重跑。
    """
    if res is None or not removed:
        return res

    res.files = [f for f in res.files if f[0] not in removed]
    res.scanned = len(res.files)
    for p in list(removed):
        res.hash_map.pop(p, None)

    # ---------- 内容重复 / 同名：按元组过滤，不足 2 个的组整组消失 ----------
    res.content_groups = [g for g in
                          ([f for f in g if f[0] not in removed] for g in res.content_groups)
                          if len(g) >= 2]
    res.name_groups = [g for g in
                       ([f for f in g if f[0] not in removed] for g in res.name_groups)
                       if len(g) >= 2]

    # ---------- 相似视频：筛成员 + 筛边，必要时按剩余边重连子组 ----------
    # 删掉的文件可能是把两块粘在一起的唯一桥梁，所以要在剩余「边」上
    # 重新做连通；包含关系建边时就已计入 edges，只看边即可完整还原分组。
    new_groups = []
    for group in res.video_groups:
        items = [r for r in group["items"] if r["path"] not in removed]
        if len(items) < 2:
            continue
        paths = {r["path"] for r in items}
        edges = [e for e in group.get("edges", [])
                 if e[0] in paths and e[1] in paths]
        contains = {k: v for k, v in group.get("contains", {}).items()
                    if k[0] in paths and k[1] in paths}
        by_path = {r["path"]: r for r in items}

        parent = {p: p for p in paths}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for m1, m2, _sim in edges:
            r1, r2 = find(m1), find(m2)
            if r1 != r2:
                parent[r2] = r1

        subs: dict = defaultdict(list)
        for p in paths:
            subs[find(p)].append(p)

        for members in subs.values():
            if len(members) < 2:
                continue                    # 彻底孤立：不再构成任何组
            ms = set(members)
            sub_edges = [e for e in edges if e[0] in ms and e[1] in ms]
            sub_contains = {k: v for k, v in contains.items()
                            if k[0] in ms and k[1] in ms}
            rows = []
            for p in members:               # 按剩余成员重算每行的关系与相似度
                r = dict(by_path[p])
                touched_edges = [e[2] for e in sub_edges if p in (e[0], e[1])]
                if any((p, other) in sub_contains for other in ms):
                    r["relation"] = "contained"
                elif any((other, p) in sub_contains for other in ms):
                    r["relation"] = "container"
                else:
                    r["relation"] = "same"
                r["sim"] = (sum(touched_edges) / len(touched_edges) * 100.0) \
                    if touched_edges else r.get("sim")
                rows.append(r)
            rows.sort(key=lambda x: x["size"], reverse=True)
            sims = [e[2] for e in sub_edges]
            new_groups.append({
                "items": rows,
                "dur": max(r["dur"] for r in rows),
                "avg_sim": (sum(sims) / len(sims) * 100.0) if sims else None,
                "kind": "contains" if sub_contains else "same",
                "contains": sub_contains,
                "edges": sub_edges,
            })
    new_groups.sort(key=lambda g: max(r["size"] for r in g["items"]), reverse=True)
    res.video_groups = new_groups
    return res


def scan_folder(
    root: str,
    include_sub: bool = True,
    min_size: int = 0,
    ignore_ext: bool = False,
    need_content: bool = True,
    need_name: bool = True,
    need_similar: bool = False,
    video_frames: int = DEFAULT_FRAMES,
    video_threshold: float = DEFAULT_DIST,
    video_duration_tol: float = DEFAULT_DURATION_TOL,
    video_min_seconds: float = DEFAULT_MIN_SECONDS,
    video_dense: bool = False,
    video_dense_interval: float = 2.0,
    video_decode_mode: str = "auto",
    status=None,
    stop: threading.Event | None = None,
) -> ScanResult:
    """遍历文件夹并分组，返回 ScanResult。status 是进度回调，stop 用于中途停止。"""
    result = ScanResult()
    emit = status if status is not None else (lambda _msg: None)
    t0 = time.time()

    def stopped() -> bool:
        return stop is not None and stop.is_set()

    # ---------- 第一步：遍历目录，收集文件 ----------
    # 用 os.scandir 而不是 os.walk + os.stat：目录项的类型和大小在枚举时
    # 就一并返回，每个文件少一次系统调用，遍历大量文件时磁盘压力明显更小。
    emit("正在遍历文件夹…")
    dirs = 0
    seen = 0
    stack = [root]
    while stack:
        if stopped():
            result.stopped = True
            return result
        dirpath = stack.pop()
        dirs += 1
        try:
            with os.scandir(dirpath) as it:
                for entry in it:
                    seen += 1
                    # 单个目录里几十万个文件时也能中途停下
                    if seen % 2000 == 0 and stopped():
                        result.stopped = True
                        return result
                    try:
                        if entry.is_symlink():
                            # 与 os.walk 默认行为一致：不下钻目录符号链接；
                            # 文件符号链接照常统计（stat 跟随到真实文件）
                            if entry.is_dir(follow_symlinks=True):
                                continue
                        elif entry.is_dir(follow_symlinks=False):
                            if include_sub:
                                stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=True):
                            continue
                        st = entry.stat(follow_symlinks=True)
                        if st.st_size < min_size:
                            continue
                        result.files.append((entry.path, st.st_size, entry.name))
                    except OSError as exc:
                        result.errors.append(f"{entry.path}: {exc}")
        except OSError as exc:
            result.errors.append(f"{dirpath}: {exc}")
        if dirs % 10 == 0:
            emit(f"正在遍历文件夹… 已找到 {len(result.files):,} 个文件")
    result.scanned = len(result.files)

    # ---------- 第二步：找内容完全相同的重复文件 ----------
    if need_content and not stopped():
        size_count = Counter(sz for _, sz, _ in result.files)
        candidates = [f for f in result.files if size_count[f[1]] > 1]  # 只有大小相同的才可能重复
        if candidates:
            emit(f"正在比对 {len(candidates):,} 个大小相同的候选文件…")
            head_groups: dict[tuple[int, str], list[str]] = defaultdict(list)
            for i, (path, size, _n) in enumerate(candidates, 1):
                if stopped():
                    result.stopped = True
                    return result
                try:
                    head_groups[(size, read_hash(path, HEAD_LIMIT, stop))].append(path)
                except ScanAborted:
                    result.stopped = True
                    return result
                except OSError as exc:
                    result.errors.append(f"{path}: {exc}")
                if i % 200 == 0:
                    emit(f"正在比对内容… {i:,}/{len(candidates):,}")

            # 文件头相同的再算完整哈希（更慢，但只对真正可能的重复做）
            final: dict[tuple[int, str], list[str]] = defaultdict(list)
            need_full = sum(len(v) for v in head_groups.values() if len(v) > 1)
            done = 0
            for (size, head), paths in head_groups.items():
                if len(paths) == 1:
                    continue
                if size <= HEAD_LIMIT:           # 小文件读头就等于读全文
                    for p in paths:
                        result.hash_map[p] = head
                        final[(size, head)].append(p)
                    continue
                for p in paths:
                    if stopped():
                        result.stopped = True
                        return result
                    try:
                        digest = read_hash(p, stop=stop)
                    except ScanAborted:
                        result.stopped = True
                        return result
                    except OSError as exc:
                        result.errors.append(f"{p}: {exc}")
                        continue
                    result.hash_map[p] = digest
                    final[(size, digest)].append(p)
                    done += 1
                    if done % 50 == 0 and need_full:
                        emit(f"正在校验完整内容… {done:,}/{need_full:,}")

            for (size, _digest), paths in final.items():
                if len(paths) > 1:
                    result.content_groups.append(
                        [(p, size, os.path.basename(p)) for p in paths]
                    )
            # 顺手记下同名组里可能用到的小文件哈希
            for (size, head), paths in head_groups.items():
                if len(paths) == 1 and size <= HEAD_LIMIT:
                    result.hash_map[paths[0]] = head

    # ---------- 第三步：找文件名相同的文件 ----------
    if need_name and not stopped():
        emit("正在按文件名分组…")
        by_name: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
        for item in result.files:
            by_name[name_key(item[2], ignore_ext)].append(item)
        for items in by_name.values():
            if len(items) > 1:
                result.name_groups.append(items)

        # 同名组里大小完全一致的，补算内容哈希，用来标注「内容也相同」
        todo = [p for items in result.name_groups
                if len({sz for _, sz, _ in items}) == 1
                for p, _sz, _n in items if p not in result.hash_map]
        if todo:
            emit(f"正在校验同名文件的内容… 0/{len(todo)}")
            for i, path in enumerate(todo, 1):
                if stopped():
                    result.stopped = True
                    return result
                try:
                    result.hash_map[path] = read_hash(path, stop=stop)
                except ScanAborted:
                    result.stopped = True
                    return result
                except OSError as exc:
                    result.errors.append(f"{path}: {exc}")
                if i % 50 == 0:
                    emit(f"正在校验同名文件的内容… {i:,}/{len(todo):,}")

    # ---------- 第四步：相似视频（抽帧 + 感知哈希）----------
    if need_similar and not stopped():
        try:
            has_video = any(os.path.splitext(f[2])[1].lower() in VIDEO_EXTS and f[1] >= 1024
                            for f in result.files)
            if not has_video:
                result.video_note = "没有找到视频文件"
            else:
                result.video_groups = scan_similar_videos(
                    result.files,
                    frame_count=video_frames,
                    threshold=video_threshold,
                    duration_tol=video_duration_tol,
                    min_seconds=video_min_seconds,
                    dense=video_dense,
                    dense_interval=video_dense_interval,
                    decode_mode=video_decode_mode,
                    status=emit,
                    stop=stop,
                )
                if stop is not None and stop.is_set():
                    # 视频阶段被「停止」打断：如实标注结果不完整，
                    # 不要再报「没有发现相似视频」这种误导性的结论
                    result.stopped = True
                elif not result.video_groups:
                    result.video_note = "没有发现相似视频"
        except RuntimeError as exc:
            result.video_note = str(exc)
        except Exception as exc:
            result.video_note = f"相似视频检测失败：{type(exc).__name__}: {exc}"

    result.seconds = time.time() - t0
    return result


# --------------------------------------------------------------------------
# 图形界面
# --------------------------------------------------------------------------
class App:
    def __init__(self, master: tk.Tk | None = None) -> None:
        self.root = master or tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("1180x700")
        self.root.minsize(900, 520)

        self.queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.menu_item = None
        self.result: ScanResult | None = None
        self.group_meta: dict[str, tuple[str, int]] = {}   # tree item -> (类型, 组号)

        self.folder_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="content")
        self.sub_var = tk.BooleanVar(value=True)
        self.skip_empty_var = tk.BooleanVar(value=True)
        self.ignore_ext_var = tk.BooleanVar(value=False)
        self.min_kb_var = tk.StringVar(value="0")
        self.sort_var = tk.StringVar(value="大小降序（大 → 小）")
        self.status_var = tk.StringVar(value="请选择要扫描的文件夹")
        self.similar_var = tk.BooleanVar(value=False)
        self.frames_var = tk.StringVar(value=str(DEFAULT_FRAMES))
        self.dist_var = tk.StringVar(value=str(int(DEFAULT_DIST)))
        self.dtol_var = tk.StringVar(value=str(int(DEFAULT_DURATION_TOL * 100)))
        self.vmin_var = tk.StringVar(value=str(int(DEFAULT_MIN_SECONDS)))
        self.dense_var = tk.BooleanVar(value=False)
        self.dense_interval_var = tk.StringVar(value="2")
        # 解码方式：auto / gpu / cpu（见 wants_gpu、route_bytes_for）
        self.decode_var = tk.StringVar(value=DECODE_MODES[0][0])

        self._build_ui()

        # 没有 OpenCV 就禁用相似视频与逐帧/合集检测（其余功能照常）
        self.cv2_available = load_cv2() is not None
        if not self.cv2_available:
            self.similar_chk.configure(state="disabled")
            self.dense_chk.configure(state="disabled")
            self.similar_var.set(False)
            self.dense_var.set(False)
            self.status_var.set("提示：未检测到 OpenCV（cv2），「相似视频 / 逐帧合集」模式已禁用；查重与查同名不受影响")

        self.root.after(100, self._poll_queue)

    # ---------------- 界面搭建 ----------------
    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24)
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"))

        pad = {"padx": 6, "pady": 4}

        # 第 1 行：文件夹选择
        row1 = ttk.Frame(self.root)
        row1.pack(fill="x", **pad)
        ttk.Label(row1, text="文件夹：").pack(side="left")
        entry = ttk.Entry(row1, textvariable=self.folder_var)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(row1, text="选择文件夹…", command=self.choose_folder).pack(side="left")
        self.btn_start = ttk.Button(row1, text="开始扫描", command=self.start_scan)
        self.btn_start.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(row1, text="停止", command=self.stop_scan, state="disabled")
        self.btn_stop.pack(side="left")

        # 第 2 行：扫描选项
        row2 = ttk.Frame(self.root)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="查找：").pack(side="left")
        ttk.Radiobutton(row2, text="内容相同（重复文件）", value="content",
                        variable=self.mode_var).pack(side="left")
        ttk.Radiobutton(row2, text="文件名相同", value="name",
                        variable=self.mode_var).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(row2, text="两者都查", value="both",
                        variable=self.mode_var).pack(side="left", padx=(6, 12))

        ttk.Checkbutton(row2, text="包含子文件夹", variable=self.sub_var).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(row2, text="忽略空文件", variable=self.skip_empty_var).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(row2, text="同名比较忽略扩展名", variable=self.ignore_ext_var).pack(side="left", padx=(0, 12))
        ttk.Label(row2, text="最小文件(KB)：").pack(side="left")
        ttk.Entry(row2, textvariable=self.min_kb_var, width=8).pack(side="left", padx=(0, 12))
        ttk.Label(row2, text="排序：").pack(side="left")
        sort_box = ttk.Combobox(row2, textvariable=self.sort_var, width=18, state="readonly",
                                values=["大小降序（大 → 小）", "大小升序（小 → 大）"])
        sort_box.pack(side="left")
        sort_box.bind("<<ComboboxSelected>>", lambda _e: self.render())

        # 第 3 行：相似视频（感知比对）
        row3 = ttk.Frame(self.root)
        row3.pack(fill="x", **pad)
        self.similar_chk = ttk.Checkbutton(
            row3, variable=self.similar_var,
            text="相似视频（感知比对：抓 1080p/720p 等同一内容的不同版本）",
        )
        self.similar_chk.pack(side="left")
        ttk.Label(row3, text="抽帧数：").pack(side="left", padx=(12, 2))
        ttk.Spinbox(row3, from_=4, to=48, width=4, textvariable=self.frames_var).pack(side="left")
        ttk.Label(row3, text="平均汉明距离 ≤：").pack(side="left", padx=(10, 2))
        ttk.Spinbox(row3, from_=0, to=32, width=4, textvariable=self.dist_var).pack(side="left")
        ttk.Label(row3, text="时长容差%：").pack(side="left", padx=(10, 2))
        ttk.Spinbox(row3, from_=1, to=50, width=4, textvariable=self.dtol_var).pack(side="left")
        ttk.Label(row3, text="跳过短于(秒)：").pack(side="left", padx=(10, 2))
        ttk.Spinbox(row3, from_=0, to=600, width=5, textvariable=self.vmin_var).pack(side="left")

        # 第 4 行：逐帧/合集包含检测
        row4 = ttk.Frame(self.root)
        row4.pack(fill="x", **pad)
        self.dense_chk = ttk.Checkbutton(
            row4, variable=self.dense_var,
            text="逐帧/合集包含检测（找「合集里包含了哪些视频」，按固定间隔抽帧）",
        )
        self.dense_chk.pack(side="left")
        ttk.Label(row4, text="抽帧间隔(秒)：").pack(side="left", padx=(12, 2))
        ttk.Spinbox(row4, from_=1, to=10, width=4, textvariable=self.dense_interval_var).pack(side="left")
        ttk.Label(row4, text="（间隔越小越精确但越慢；合集很长时会自动放大）",
                  foreground="#666666").pack(side="left", padx=(8, 0))

        # 第 5 行：解码方式（提速 + 降 CPU 占用）
        row5 = ttk.Frame(self.root)
        row5.pack(fill="x", **pad)
        ttk.Label(row5, text="逐帧解码：").pack(side="left")
        self.decode_box = ttk.Combobox(row5, textvariable=self.decode_var, width=30,
                                       state="readonly",
                                       values=[label for label, _ in DECODE_MODES])
        self.decode_box.pack(side="left", padx=(2, 8))
        self.ffmpeg_hint = ttk.Label(row5, text="", foreground="#666666")
        self.ffmpeg_hint.pack(side="left")
        ff = find_ffmpeg()
        if ff:
            self.ffmpeg_hint.configure(
                text=f"已找到 ffmpeg：{os.path.basename(ff)}（顺序解码提速、低优先级不抢前台）")
        else:
            self.ffmpeg_hint.configure(
                foreground="#b36b00",
                text="未找到 ffmpeg.exe：把 ffmpeg.exe 放到本工具同目录可大幅提速并降低 CPU 占用")

        # 中间：结果树
        table = ttk.Frame(self.root)
        table.pack(fill="both", expand=True, **pad)
        columns = ("size", "bytes", "info", "path")
        self.tree = ttk.Treeview(table, columns=columns, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="文件 / 分组")
        self.tree.heading("size", text="大小")
        self.tree.heading("bytes", text="字节数")
        self.tree.heading("info", text="分辨率/时长/相似度")
        self.tree.heading("path", text="完整路径")
        self.tree.column("#0", width=280, minwidth=160)
        self.tree.column("size", width=95, anchor="e", stretch=False)
        self.tree.column("bytes", width=115, anchor="e", stretch=False)
        self.tree.column("info", width=195, anchor="w", stretch=False)
        self.tree.column("path", width=470, minwidth=200)

        vsb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)

        self.tree.tag_configure("section", background="#d9e7ff", font=("Microsoft YaHei UI", 10, "bold"))
        self.tree.tag_configure("group", background="#eef4ff", font=("Microsoft YaHei UI", 9, "bold"))
        self.tree.tag_configure("file", foreground="#10304f")
        self.tree.tag_configure("file_alt", background="#fafafa", foreground="#10304f")

        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Button-3>", self.on_right_click)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="打开文件", command=self.menu_open)
        self.menu.add_command(label="打开所在文件夹", command=self.menu_reveal)
        self.menu.add_separator()
        self.menu.add_command(label="复制该文件路径", command=self.menu_copy_path)
        self.menu.add_command(label="复制所选文件的全部路径", command=self.menu_copy_all)
        self.menu.add_separator()
        self.menu.add_command(label="删除该文件（移到回收站）", command=self.menu_delete)

        # 底部：状态 + 进度 + 导出
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)
        ttk.Label(bottom, textvariable=self.status_var, anchor="w").pack(
            side="left", fill="x", expand=True
        )
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=180)
        self.progress.pack(side="left", padx=8)
        ttk.Button(bottom, text="导出报告(CSV)", command=self.export_csv).pack(side="left", padx=4)
        ttk.Button(bottom, text="复制结果", command=self.copy_report).pack(side="left")

        # 提示
        tip = ttk.Label(
            self.root,
            text="提示：双击某一行可打开所在文件夹；右键可打开文件 / 复制路径 / 删除。删除会先征求确认（Windows 下移入回收站）。"
                 "相似视频只读取视频抽帧，不会改动文件；分析结果会缓存，第二次检查同一批视频快得多。",
            foreground="#666666",
        )
        tip.pack(fill="x", padx=8, pady=(0, 6))

    # ---------------- 扫描流程 ----------------
    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择要扫描的文件夹")
        if folder:
            self.folder_var.set(os.path.normpath(folder))

    def _min_size_bytes(self) -> int:
        raw = self.min_kb_var.get().strip()
        try:
            kb = max(0.0, float(raw))
        except ValueError:
            kb = 0.0
        size = int(kb * 1024)
        if self.skip_empty_var.get():
            size = max(size, 1)
        return size

    @staticmethod
    def _num(var, default: float, lo: float, hi: float, cast=int):
        """安全读取数字输入框，超范围或非法就回落到默认值"""
        try:
            value = cast(float(var.get()))
        except (ValueError, TypeError):
            value = cast(default)
        return max(cast(lo), min(cast(hi), value))

    def _decode_code(self) -> str:
        """把界面上的「逐帧解码」标签换成内部代码（auto/gpu/cpu）。"""
        label = self.decode_var.get()
        for text, code in DECODE_MODES:
            if text == label:
                return code
        return "auto"

    def start_scan(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        folder = self.folder_var.get().strip().strip('"')
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning(APP_TITLE, "请先选择一个存在的文件夹。")
            return

        mode = self.mode_var.get()
        self.result = None
        self.group_meta.clear()
        self.tree.delete(*self.tree.get_children())
        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress.start(25)
        self.status_var.set("正在扫描…")

        args = dict(
            root=folder,
            include_sub=self.sub_var.get(),
            min_size=self._min_size_bytes(),
            ignore_ext=self.ignore_ext_var.get(),
            need_content=mode in ("content", "both"),
            need_name=mode in ("name", "both"),
            need_similar=(self.similar_var.get() or self.dense_var.get()) and self.cv2_available,
            video_frames=self._num(self.frames_var, DEFAULT_FRAMES, 4, 48),
            video_threshold=float(self._num(self.dist_var, DEFAULT_DIST, 0, 32)),
            video_duration_tol=self._num(self.dtol_var, DEFAULT_DURATION_TOL * 100, 1, 50) / 100.0,
            video_min_seconds=float(self._num(self.vmin_var, DEFAULT_MIN_SECONDS, 0, 600)),
            video_dense=self.dense_var.get(),
            video_dense_interval=float(self._num(self.dense_interval_var, 2.0, 1, 10, cast=float)),
            video_decode_mode=self._decode_code(),
            status=lambda msg: self.queue.put(("status", msg)),
            stop=self.stop_event,
        )

        def job() -> None:
            try:
                lower_thread_priority()       # 扫描线程让路，不跟前台程序抢 CPU
                res = scan_folder(**args)
                self.queue.put(("result", res))
            except Exception as exc:  # 兜底，避免线程静默死掉
                self.queue.put(("error", f"{type(exc).__name__}: {exc}"))

        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def stop_scan(self) -> None:
        self.stop_event.set()
        self.btn_stop.configure(state="disabled")   # 防止重复点击
        self.status_var.set("正在停止…（当前这一步收尾，通常一两秒内）")

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                try:
                    if kind == "status":
                        # 已请求停止：不再刷新进度数字，否则看起来像“没停下来还在继续”
                        if self.stop_event.is_set():
                            continue
                        self.status_var.set(payload)
                    elif kind == "result":
                        self.on_result(payload)
                    elif kind == "error":
                        self.finish_controls()
                        messagebox.showerror(APP_TITLE, f"扫描出错：\n{payload}")
                except Exception as exc:      # 单个消息出错不能拖垮轮询
                    self.finish_controls()
                    self.status_var.set(f"处理结果时出错：{type(exc).__name__}: {exc}")
        except queue.Empty:
            pass
        finally:                              # 无论如何都要继续轮询
            self.root.after(100, self._poll_queue)

    def finish_controls(self) -> None:
        self.progress.stop()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    def on_result(self, res: ScanResult) -> None:
        self.result = res
        self.render()
        self.finish_controls()
        self.status_var.set("；".join(self._summary_parts(res)))

    def _summary_parts(self, res: ScanResult) -> list:
        parts = [f"共扫描 {res.scanned:,} 个文件，用时 {res.seconds:.1f} 秒"]
        if res.content_groups:
            dup_files = sum(len(g) for g in res.content_groups)
            parts.append(f"内容重复 {len(res.content_groups)} 组（{dup_files} 个文件）")
            parts.append(f"可清理空间约 {human_size(res.wasted_bytes)}")
        elif self.mode_var.get() in ("content", "both"):
            parts.append("没有发现内容重复的文件")
        if res.name_groups:
            parts.append(f"同名 {len(res.name_groups)} 组")
        elif self.mode_var.get() in ("name", "both"):
            parts.append("没有发现同名文件")
        if self.similar_var.get() or self.dense_var.get():
            if res.video_groups:
                video_count = sum(len(g["items"]) for g in res.video_groups)
                label = "相似/合集" if self.dense_var.get() else "相似视频"
                parts.append(f"{label} {len(res.video_groups)} 组（{video_count} 个文件）")
            elif res.video_note:
                parts.append(res.video_note)
        if res.errors:
            parts.append(f"跳过 {len(res.errors)} 个无法读取的项")
        if res.stopped:
            parts.append("（已手动停止，结果不完整）")
        if not res.content_groups and not res.name_groups and not res.video_groups:
            parts.append("未发现重复、同名或相似的文件")
        return parts

    # ---------------- 结果展示 ----------------
    def _sorted_groups(self, groups):
        desc = self.sort_var.get().startswith("大小降序")
        return sorted(groups, key=lambda g: max(sz for _, sz, _ in g), reverse=desc)

    def render(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.group_meta.clear()
        res = self.result
        if res is None:
            return
        desc = self.sort_var.get().startswith("大小降序")
        show_all = (len(res.content_groups) + len(res.name_groups) + len(res.video_groups)) <= 40

        if res.content_groups:
            total = sum(sz for g in res.content_groups for _, sz, _ in g)
            sec = self.tree.insert(
                "", "end", text=f"■ 内容相同的重复文件（{len(res.content_groups)} 组，"
                                f"合计 {human_size(total)}）",
                values=("", "", "", ""), open=show_all, tags=("section",),
            )
            for i, group in enumerate(self._sorted_groups(res.content_groups), 1):
                self._insert_group(sec, "内容重复", i, group, desc)

        if res.name_groups:
            sec = self.tree.insert(
                "", "end", text=f"■ 文件名相同的文件（{len(res.name_groups)} 组）",
                values=("", "", "", ""), open=show_all, tags=("section",),
            )
            for i, group in enumerate(self._sorted_groups(res.name_groups), 1):
                self._insert_group(sec, "同名", i, group, desc, check_identical=True)

        if res.video_groups:
            videos = sum(len(g["items"]) for g in res.video_groups)
            sec = self.tree.insert(
                "", "end", text=f"■ 相似的视频（{len(res.video_groups)} 组，共 {videos} 个文件；"
                                f"感知比对，非逐字节相同）",
                values=("", "", "", ""), open=show_all, tags=("section",),
            )
            groups = sorted(res.video_groups, key=lambda g: max(r["size"] for r in g["items"]),
                            reverse=desc)
            for i, group in enumerate(groups, 1):
                self._insert_video_group(sec, i, group)

    def _insert_group(self, parent, kind, index, group, desc, check_identical=False):
        items = sorted(group, key=lambda f: f[1], reverse=desc)
        total = sum(sz for _, sz, _ in items)
        note = ""
        if check_identical:
            sizes = {sz for _, sz, _ in items}
            hashes = {self.result.hash_map.get(p) for p, _, _ in items} if self.result else set()
            if len(sizes) == 1 and None not in hashes and len(hashes) == 1:
                note = "　·　内容也完全相同"
        title = (f"第 {index} 组 · {len(items)} 个文件 · 合计 {human_size(total)}"
                 f"（{'大的在前' if desc else '小的在前'}）{note}")
        node = self.tree.insert(
            parent, "end", text=title,
            values=(human_size(total), f"{total:,}", "", ""),
            open=True, tags=("group",),
        )
        self.group_meta[node] = (kind, index)
        for n, (path, size, name) in enumerate(items):
            child = self.tree.insert(
                node, "end", text=name,
                values=(human_size(size), f"{size:,}", "", path),
                tags=("file" if n % 2 == 0 else "file_alt",),
            )
            self.group_meta[child] = (kind, index, path)

    def _insert_video_group(self, parent, index, group):
        """相似视频分组：组标题给时长/相似度/类型，文件行给分辨率/时长/相似度/关系/包含位置"""
        items = group["items"]
        biggest = items[0]
        kind = group.get("kind", "same")
        contains = group.get("contains", {})

        kind_label = "合集包含" if kind == "contains" else "同内容"
        title = f"第 {index} 组 · {len(items)} 个视频 · [{kind_label}] · 时长约 {mmss(group['dur'])}"
        if group.get("avg_sim") is not None:
            title += f" · 平均相似度 {group['avg_sim']:.1f}%"
        node = self.tree.insert(
            parent, "end", text=title,
            values=(human_size(sum(r["size"] for r in items)),
                    f"{sum(r['size'] for r in items):,}", "", ""),
            open=True, tags=("group",),
        )
        self.group_meta[node] = ("相似视频", index)

        # 反查：某个文件被哪个合集包含、在什么位置
        def location_of(path):
            for (ps, pl), hit in contains.items():
                if ps == path:
                    host = None
                    for r in items:
                        if r["path"] == pl:
                            host = r["name"]
                            break
                    return f"⊂ 在「{host or pl}」第 {mmss(hit['start'])} 处" if host else None
            return None

        for n, row in enumerate(items):
            sim = f"相似度 {row['sim']:.1f}%" if row["sim"] is not None else "相似度 —"
            relation = row.get("relation", "same")
            mark = ""
            if relation == "container":
                mark = "【合集】"
            elif relation == "contained":
                mark = "【片段】"
            elif row is biggest:
                mark = "（体积最大，建议保留）"
            loc = location_of(row["path"])
            info = f"{row['w']}×{row['h']}　{mmss(row['dur'])}　{sim}{mark}"
            if loc:
                info += f"　{loc}"
            child = self.tree.insert(
                node, "end", text=row["name"],
                values=(human_size(row["size"]), f"{row['size']:,}", info, row["path"]),
                tags=("file" if n % 2 == 0 else "file_alt",),
            )
            self.group_meta[child] = ("相似视频", index, row["path"])

    # ---------------- 交互 ----------------
    def _paths_of(self, item) -> list[str]:
        meta = self.group_meta.get(item)
        if meta is None:
            return []
        if len(meta) == 3:
            return [meta[2]]
        paths = []
        for child in self.tree.get_children(item):
            paths.extend(self._paths_of(child))
        return paths

    def on_double_click(self, _event) -> None:
        item = self.tree.focus()
        meta = self.group_meta.get(item) if item else None
        # 只有文件行响应：双击 = 用默认播放器打开这个文件。
        # 组/分区行的展开折叠交给 Treeview 自带的双击行为，不重复处理。
        if meta is not None and len(meta) == 3:
            open_file(meta[2])

    def on_right_click(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self.tree.focus(item)
            self.menu_item = item          # 记住右键点中的行：菜单动作以它为准
            self.menu.tk_popup(event.x_root, event.y_root)

    def _menu_paths(self) -> list[str]:
        """右键/菜单作用的行。优先用鼠标点中的那一行，
        否则多选时 selection() 的第一项可能是别的行（显示对、打开错就出在这）"""
        item = getattr(self, "menu_item", None)
        if item and item in self.tree.selection():
            paths = self._paths_of(item)
            if paths:
                return paths
        return self._selected_paths()

    def _selected_paths(self) -> list[str]:
        paths: list[str] = []
        for item in self.tree.selection():
            for p in self._paths_of(item):
                if p not in paths:
                    paths.append(p)
        return paths

    def menu_open(self) -> None:
        paths = self._menu_paths()
        if paths:
            open_file(paths[0])

    def menu_reveal(self) -> None:
        paths = self._menu_paths()
        if paths:
            reveal_in_explorer(paths[0])

    def menu_copy_path(self) -> None:
        paths = self._selected_paths()
        if paths:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(paths))
            self.status_var.set(f"已复制 {len(paths)} 条路径")

    def menu_copy_all(self) -> None:
        self.menu_copy_path()

    def menu_delete(self) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        preview = "\n".join(paths[:6])
        if len(paths) > 6:
            preview += f"\n… 以及另外 {len(paths) - 6} 个文件"
        if not messagebox.askyesno(APP_TITLE, f"确定要删除下面 {len(paths)} 个文件吗？\n\n{preview}"):
            return
        failed = []
        for p in paths:
            try:
                if sys.platform == "win32":
                    import ctypes
                    from ctypes import wintypes

                    class SHFILEOPSTRUCTW(ctypes.Structure):
                        _fields_ = [
                            ("hwnd", wintypes.HWND),
                            ("wFunc", wintypes.UINT),
                            ("pFrom", wintypes.LPCWSTR),
                            ("pTo", wintypes.LPCWSTR),
                            ("fFlags", ctypes.c_ushort),
                            ("fAnyOperationsAborted", wintypes.BOOL),
                            ("hNameMappings", ctypes.c_void_p),
                            ("lpszProgressTitle", wintypes.LPCWSTR),
                        ]

                    op = SHFILEOPSTRUCTW()
                    op.wFunc = 3  # FO_DELETE
                    op.pFrom = p + "\0\0"
                    op.fFlags = 0x0040 | 0x0010 | 0x0004  # 回收站 + 不弹确认 + 不弹错误框
                    ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
                else:
                    os.remove(p)
            except Exception:
                failed.append(p)
        remaining = [p for p in paths if os.path.exists(p)]
        if remaining:
            messagebox.showwarning(APP_TITLE, f"有 {len(remaining)} 个文件未能删除。")
        deleted = set(paths) - set(remaining)
        if not deleted:
            return
        if self.result is not None and (self.worker is None or not self.worker.is_alive()):
            # 关键：删一个文件不可能让剩下文件之间冒出新关系，
            # 所以直接在内存结果里剔除即可，不必重新遍历/抽帧（原来那一下最慢）
            prune_result(self.result, deleted)
            self.render()
            parts = self._summary_parts(self.result)
            parts.insert(0, f"已删除 {len(deleted)} 个文件（结果已就地更新）")
            self.status_var.set("；".join(parts))
        else:
            self.start_scan()   # 扫描还没跑过 / 正在跑：退回重扫

    def copy_report(self) -> None:
        if not self.result:
            return
        lines = self.report_lines()
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.status_var.set("结果已复制到剪贴板")

    # ---------------- 报告 ----------------
    def report_rows(self):
        """返回 [(类型, 组号, 文件名, 大小可读, 字节, 说明, 路径), ...]"""
        rows = []
        if not self.result:
            return rows
        res = self.result
        for kind, groups in (("内容重复", res.content_groups), ("同名", res.name_groups)):
            for i, group in enumerate(self._sorted_groups(groups), 1):
                for path, size, name in sorted(group, key=lambda f: f[1], reverse=True):
                    rows.append((kind, i, name, human_size(size), size, "", path))
        desc = self.sort_var.get().startswith("大小降序")
        vgroups = sorted(res.video_groups,
                         key=lambda g: max(r["size"] for r in g["items"]), reverse=desc)
        for i, group in enumerate(vgroups, 1):
            contains = group.get("contains", {})
            for row in group["items"]:
                sim = f"{row['sim']:.1f}%" if row["sim"] is not None else "—"
                info = f"{row['w']}x{row['h']} | {mmss(row['dur'])} | 相似度 {sim}"
                relation = row.get("relation", "same")
                if relation == "container":
                    info += " | 合集"
                elif relation == "contained":
                    info += " | 片段"
                # 包含位置：这个文件被哪个合集包含、在什么位置
                for (ps, pl), hit in contains.items():
                    if ps == row["path"]:
                        host = next((r["name"] for r in group["items"] if r["path"] == pl), pl)
                        info += f" | 在「{host}」第 {mmss(hit['start'])} 处"
                rows.append(("相似视频", i, row["name"], human_size(row["size"]),
                             row["size"], info, row["path"]))
        return rows

    def report_lines(self) -> list[str]:
        lines = [
            f"{APP_TITLE} — 扫描报告",
            f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"状态：{self.status_var.get()}",
            "-" * 100,
        ]
        current = None
        if self.result:
            for kind, idx, name, readable, size, info, path in self.report_rows():
                tag = (kind, idx)
                if tag != current:
                    current = tag
                    lines.append("")
                    lines.append(f"【{kind}】第 {idx} 组")
                lines.append(f"    {readable:>10}  {info:<34}  {path}" if info
                             else f"    {readable:>10}  {path}")
        return lines

    def export_csv(self) -> None:
        if not self.result:
            messagebox.showinfo(APP_TITLE, "还没有扫描结果，请先扫描。")
            return
        path = filedialog.asksaveasfilename(
            title="保存报告", defaultextension=".csv",
            initialfile="重复文件报告.csv",
            filetypes=[("CSV 文件", "*.csv"), ("文本文件", "*.txt")],
        )
        if not path:
            return
        try:
            if path.lower().endswith(".txt"):
                with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                    fh.write("\n".join(self.report_lines()))
            else:
                with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["类型", "组号", "文件名", "大小", "字节数", "说明", "完整路径"])
                    writer.writerows(self.report_rows())
            self.status_var.set(f"报告已导出：{path}")
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"保存失败：{exc}")

    def run(self) -> None:
        self.root.mainloop()


# --------------------------------------------------------------------------
# 命令行自检（不打开界面）：python duplicate_finder.py --selftest <文件夹>
# --------------------------------------------------------------------------
def selftest(folder: str) -> int:
    try:  # 让控制台也能正确显示中文
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # --dense 开启逐帧/合集包含检测；--decode=cpu|gpu 指定解码方式（默认 auto）
    dense = "--dense" in sys.argv
    dmode = "auto"
    for a in sys.argv:
        if a.startswith("--decode="):
            dmode = a.split("=", 1)[1].strip().lower() or "auto"
    if dmode not in ("auto", "cpu", "gpu"):
        dmode = "auto"
    res = scan_folder(folder, include_sub=True, min_size=1, need_content=True,
                      need_name=True, need_similar=True,
                      video_dense=dense, video_dense_interval=2.0,
                      video_decode_mode=dmode,
                      status=lambda msg: print("  ·", msg))
    print(f"扫描文件数: {res.scanned}  用时 {res.seconds:.2f}s")
    print(f"内容重复组: {len(res.content_groups)}   可清理: {human_size(res.wasted_bytes)}")
    for i, group in enumerate(sorted(res.content_groups, key=lambda g: max(s for _, s, _ in g), reverse=True), 1):
        print(f"  [重复] 第 {i} 组，{len(group)} 个文件")
        for path, size, _n in sorted(group, key=lambda f: f[1], reverse=True):
            print(f"      {human_size(size):>10}  {path}")
    print(f"同名组: {len(res.name_groups)}")
    for i, group in enumerate(sorted(res.name_groups, key=lambda g: max(s for _, s, _ in g), reverse=True), 1):
        print(f"  [同名] 第 {i} 组，{len(group)} 个文件")
        for path, size, _n in sorted(group, key=lambda f: f[1], reverse=True):
            print(f"      {human_size(size):>10}  {path}")
    if res.video_groups:
        print(f"相似/合集视频组: {len(res.video_groups)}")
        for i, group in enumerate(res.video_groups, 1):
            avg = f"{group['avg_sim']:.1f}%" if group.get("avg_sim") is not None else "—"
            kind = "合集包含" if group.get("kind") == "contains" else "同内容"
            print(f"  [{kind}] 第 {i} 组，{len(group['items'])} 个，"
                  f"时长约 {mmss(group['dur'])}，平均相似度 {avg}")
            for row in group["items"]:
                sim = f"{row['sim']:.1f}%" if row["sim"] is not None else "—"
                rel = {"same": "", "container": "[合集]", "contained": "[片段]"}.get(row.get("relation"), "")
                print(f"      {human_size(row['size']):>10}  {row['w']}x{row['h']}  "
                      f"{mmss(row['dur'])}  相似度 {sim:>6}{rel}  {row['path']}")
            for (ps, pl), hit in group.get("contains", {}).items():
                print(f"      ↳ 「{os.path.basename(ps)}」在「{os.path.basename(pl)}」"
                      f"第 {mmss(hit['start'])} 处（覆盖 {hit['coverage']*100:.0f}%）")
    elif res.video_note:
        print(f"相似视频: {res.video_note}")
    if res.errors:
        print(f"跳过 {len(res.errors)} 项，例如：{res.errors[:3]}")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        # 第 2 个参数是文件夹；后面可能还有 --dense 之类的开关，不能被当成路径
        folder = "."
        for arg in sys.argv[2:]:
            if not arg.startswith("--"):
                folder = arg
                break
        return selftest(folder)
    App().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
