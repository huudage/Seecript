# Seecript · Remotion 包装轨

负责把『字幕 / 标题条 / 贴纸 / 转场 / 封面』渲染成透明 WebM，
再由后端 `services/video/ffmpeg.overlay` 叠加到 FFmpeg 主轨。

## 启动 Studio（开发调试）

```bash
cd remotion
npm install
npm run studio
```

## 后端调用方式

`services/video/remotion.render_packaging_track(props, dst)` 会拼命令：

```bash
npx remotion render PackagingTrack out.webm \
  --props=out.props.json --pixel-format=yuva420p --codec=vp8 --quiet
```

`props` 结构与 `PackagingTrack.tsx` 的 `packagingTrackSchema` 对齐
（与 `server/app/schemas.py` 的 `PackagingItem` 镜像）。
