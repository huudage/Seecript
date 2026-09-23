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

// v2 U7：≤60s，与后端上传时长上限对齐。
export const VIDEO_UPLOAD_MAX_DURATION_SECONDS = 60
