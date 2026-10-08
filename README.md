# Video Duplicate Check · 视频/文件重复查找工具

一个单文件、几乎零依赖的 Windows 小工具（Python + Tkinter）：选一个文件夹，帮你找出

1. **内容完全相同的重复文件** —— 文件名可以不同，按 BLAKE2b 逐字节校验；
2. **文件名相同的文件** —— 分布在不同子目录里的同名文件；
3. **相似的视频** —— 同一内容的不同版本，比如 1080p 与 720p、不同码率、改过名字（抽帧算感知哈希 pHash 比对）；
4. **合集包含的视频** —— 一个长视频（合集）里收录了哪些独立短视频，能定位到**第几分几秒**（密集抽帧 + 指纹序列滑动比对）。

结果按文件大小排列，标出完整路径与大小，可导出 CSV / TXT 报告，也可双击打开文件或所在文件夹。

> 详细的原理、参数、实测性能与限制说明见 **[使用说明.md](使用说明.md)**。

## 快速开始（Windows）

```bat
:: 1) 需要 Python 3.8+，仅查重/查同名的话标准库即可
python --version

:: 2) 想用「相似视频 / 合集包含」检测，装 OpenCV（会一并装上 numpy）
pip install opencv-python

:: 3) 启动
双击  启动工具.bat          :: 静默启动，不留黑窗口
双击  launch_with_log.bat   :: 启动异常时用，保留日志窗口便于排错
```

也可以用命令行直接跑：

```bat
python duplicate_finder.py
```

没装 OpenCV 也能用，只是「相似视频」相关选项会自动禁用，查重、查同名不受影响。

## 关于 ffmpeg（可选，但强烈建议）

「逐帧 / 合集包含检测」在找到 `ffmpeg.exe` 时会走 **ffmpeg 顺序解码**管线：一次顺序解完整片按间隔取帧，
比 OpenCV 逐帧 seek 快数倍，并以 IDLE 优先级运行，基本不抢前台 CPU。

**本仓库不包含 ffmpeg 二进制**（约 88 MB，超出合理的仓库体积）。请任选一种方式提供：

| 方式 | 做法 |
|---|---|
| 放到工具目录 | 把 `ffmpeg.exe` 放在本目录下，或放进 `ffmpeg\` 子目录 |
| 放进 PATH | 任意位置加入系统 PATH |
| pip 安装 | `pip install imageio-ffmpeg`，程序会自动定位其自带的 ffmpeg |
| 不提供 | 也能跑，只是逐帧检测退回较慢、较吃 CPU 的 OpenCV 方式 |

## 目录结构

```
duplicate_finder.py        主程序（单文件，约 95 KB）
启动工具.bat                静默启动器
launch_with_log.bat        带日志窗口的启动器（排错用）
使用说明.md                 完整文档：用法、原理、性能实测、限制
```

运行后在工具目录会生成 `similar_video_cache.json`（视频指纹缓存，文件一改自动失效），
已通过 `.gitignore` 排除。

## 运行环境

- Windows（启动脚本与 IDLE 优先级、`pythonw` 静默启动均针对 Windows）
- Python 3.8+
- 可选：`opencv-python`（相似视频 / 合集包含检测）、`imageio-ffmpeg` 或 `ffmpeg.exe`（加速逐帧解码）

## License

暂未声明开源许可证。如需转载或用于商业用途，请先开 Issue 联系作者。
