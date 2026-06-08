// 用浏览器解码视频元数据拿真实时长(秒);编码不被浏览器支持时返回 null,让后端 ffprobe 兜底。
// Decompose 上传卡 / Library 系统&用户上传卡共用,避免两份 copy 走样。
export async function readVideoDuration(file: File): Promise<number | null> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file)
    const video = document.createElement('video')
    video.preload = 'metadata'
    const cleanup = () => URL.revokeObjectURL(url)
    video.onloadedmetadata = () => {
      cleanup()
      const d = video.duration
      resolve(Number.isFinite(d) ? d : null)
    }
    video.onerror = () => {
      cleanup()
      resolve(null)
    }
    video.src = url
  })
}

// 3 分钟 (+ 20s 余量) 上限,与后端 _USER_VIDEO_MAX_DURATION_SECONDS / _SYSTEM_UPLOAD_MAX_DURATION_SECONDS 对齐。
// 客户端预检用 180s 是因为浏览器 HTMLVideoElement.duration 是真实视频流秒数,不掺容器封装层;
// 后端给 200s 余量是因为 ffprobe 会算上一些封装层时间。
export const VIDEO_UPLOAD_MAX_DURATION_SECONDS = 180
