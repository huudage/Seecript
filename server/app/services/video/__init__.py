"""视频处理底层能力 — 服务于拆解 Agent 与渲染流水线。

子模块：
- scene_detect.py     PySceneDetect 镜头切分
- audio_analysis.py   librosa BGM 能量曲线 + tempo + onset
- ocr.py              PaddleOCR mobile 中文字幕识别
- ffmpeg.py           FFmpeg subprocess 包装（concat / extract_frame / overlay / probe）
- remotion.py         Remotion 子进程渲染包装轨

设计统一约定：
- 每个模块顶部 try-import 重依赖（pyscenedetect / librosa / paddleocr），失败时退回 mock。
- 公共函数都接受文件路径 (str | Path)，返回 dataclass / dict —— 不依赖 Pydantic 以便测试。
- 重依赖只在被实际调用时才报错，import 阶段永不抛异常。
"""
