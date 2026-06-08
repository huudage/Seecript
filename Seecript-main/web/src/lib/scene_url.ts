import type { Material, Scene } from '@/types/schemas'

export type ResolvedSceneMedia =
  | { kind: 'video'; url: string }
  | { kind: 'image'; url: string }
  | { kind: 'text'; text: string }

/**
 * 把 Scene 解析成 Remotion Player 可直接消费的资源描述。
 *
 * 对应后端 `services/render/pipeline.py::_resolve_scene_path` 的前端等价物：
 * - user_material：从 materials 列表里按 material_id 找出 file_url（后端 upload 时回填）
 * - aigc_t2v   ：取 aigc_video_urls[0]（多段串接是 ffmpeg 那条线的事，预览仅看首段）
 * - aigc_image：scene.aigc_image_url（/aigc-images/... 同源静态文件）→ 静态图当帧渲
 * - text_card  ：返回 narration 文本，由 SceneClip 渲染纯色卡
 * - sample     ：legacy，pipeline 已废，按 text_card 兜底
 *
 * 任何缺资源（材料不在 materials、aigc 还没拿到 URL）一律降级为文字卡，
 * 避免 <Video src> 报错把整个 Player 打挂。
 */
export function resolveSceneMedia(scene: Scene, materials: Material[]): ResolvedSceneMedia {
  if (scene.source === 'user_material') {
    const m = materials.find((mat) => mat.material_id === scene.source_ref)
    if (m?.file_url && m.media_type === 'video') {
      return { kind: 'video', url: m.file_url }
    }
    if (m?.file_url && m.media_type === 'image') {
      return { kind: 'video', url: m.file_url }
    }
    return { kind: 'text', text: scene.narration?.trim() || m?.filename || '[素材缺失]' }
  }
  if (scene.source === 'aigc_t2v') {
    const url = scene.aigc_video_urls[0]
    if (url) return { kind: 'video', url }
    return { kind: 'text', text: scene.narration?.trim() || '[AIGC 生成中…]' }
  }
  if (scene.source === 'aigc_image') {
    if (scene.aigc_image_url) return { kind: 'image', url: scene.aigc_image_url }
    return { kind: 'text', text: scene.narration?.trim() || '[AI 生图中…]' }
  }
  return { kind: 'text', text: scene.narration?.trim() || '[文字卡]' }
}

