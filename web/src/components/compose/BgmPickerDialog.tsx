import { useCallback, useEffect, useRef, useState } from 'react'

import { getAsset, listBgmAssets, patchPlanBgm, uploadBgm } from '@/api/bgm'
import { cn } from '@/lib/utils'
import type { Asset, Plan, PlanId } from '@/types/schemas'

/**
 * BGM 选择/上传弹窗：
 * - 列出当前项目下已有的 BGM 资产；处于 processing 状态时实时轮询直到 ready
 * - 点击选中 → PATCH /plan/{plan_id}/bgm bgm_asset_id 绑定到当前 plan，关闭弹窗
 * - 上传新文件 → POST /api/asset/upload，新增的资产自动选中（等 librosa 分析完成）
 *
 * 设计取舍：picker UI 不在四轨板内联（保持四轨板纯展示），由父级 Compose.tsx 控制 open 状态。
 */
export function BgmPickerDialog({
  open,
  onClose,
  projectId,
  planId,
  onPlanUpdated,
}: {
  open: boolean
  onClose: () => void
  projectId: string
  planId: PlanId
  onPlanUpdated: (plan: Plan) => void
}) {
  const [assets, setAssets] = useState<Asset[]>([])
  const [loading, setLoading] = useState(false)
  const [busyAssetId, setBusyAssetId] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const refresh = useCallback(async () => {
    if (!projectId) return
    setLoading(true)
    try {
      const list = await listBgmAssets(projectId)
      setAssets(list)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载 BGM 资产失败')
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    if (!open) return
    void refresh()
  }, [open, refresh])

  // 处理 processing 状态：每 2 秒轮询一次直到全部 ready / failed
  useEffect(() => {
    if (!open) return
    const pending = assets.filter((a) => a.status === 'processing')
    if (pending.length === 0) return
    const id = window.setInterval(async () => {
      try {
        const refreshed = await Promise.all(pending.map((a) => getAsset(a.asset_id)))
        setAssets((prev) => {
          const map = new Map(prev.map((a) => [a.asset_id, a]))
          for (const a of refreshed) map.set(a.asset_id, a)
          return Array.from(map.values())
        })
      } catch {
        /* 轮询失败下一轮再试 */
      }
    }, 2000)
    return () => window.clearInterval(id)
  }, [assets, open])

  const handleUpload = useCallback(
    async (file: File) => {
      setError(null)
      setUploading(true)
      try {
        const asset = await uploadBgm(projectId, file, file.name.replace(/\.[^.]+$/, ''))
        setAssets((prev) => [asset, ...prev.filter((a) => a.asset_id !== asset.asset_id)])
      } catch (err) {
        setError(err instanceof Error ? err.message : '上传 BGM 失败')
      } finally {
        setUploading(false)
      }
    },
    [projectId],
  )

  const handlePick = useCallback(
    async (asset: Asset) => {
      if (asset.status !== 'ready') {
        setError('该 BGM 还在分析中，等就绪后再选')
        return
      }
      setError(null)
      setBusyAssetId(asset.asset_id)
      try {
        const updated = await patchPlanBgm(planId, { bgm_asset_id: asset.asset_id })
        onPlanUpdated(updated)
        onClose()
      } catch (err) {
        setError(err instanceof Error ? err.message : '绑定 BGM 失败')
      } finally {
        setBusyAssetId(null)
      }
    },
    [onClose, onPlanUpdated, planId],
  )

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onClick={onClose}
    >
      <div
        className="max-h-[80vh] w-full max-w-2xl overflow-hidden rounded-lg border border-border bg-card shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-border px-4 py-3">
          <h3 className="text-sm font-semibold">选 / 上传背景音乐</h3>
          <button
            onClick={onClose}
            className="rounded text-muted-foreground hover:text-foreground"
            aria-label="关闭"
          >
            ×
          </button>
        </header>

        <div className="space-y-3 p-4">
          {error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}

          <div className="flex items-center gap-2">
            <input
              ref={fileInputRef}
              type="file"
              accept="audio/mpeg,audio/wav,audio/aac,audio/mp4,audio/ogg,audio/x-m4a"
              hidden
              onChange={(e) => {
                const file = e.target.files?.[0]
                if (file) void handleUpload(file)
                if (fileInputRef.current) fileInputRef.current.value = ''
              }}
            />
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
              className={cn(
                'rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
                uploading && 'cursor-not-allowed opacity-60',
              )}
            >
              {uploading ? '上传中…' : '上传新曲（MP3/WAV ≤ 20MB）'}
            </button>
            <button
              onClick={() => void refresh()}
              disabled={loading}
              className="rounded-md border border-border bg-background/60 px-3 py-1.5 text-xs hover:bg-secondary disabled:opacity-60"
            >
              {loading ? '刷新中…' : '刷新'}
            </button>
          </div>

          <div className="max-h-[50vh] space-y-1.5 overflow-y-auto">
            {assets.length === 0 && !loading && (
              <p className="rounded-md border border-dashed border-border bg-background/30 px-3 py-6 text-center text-xs text-muted-foreground">
                项目里还没有背景音乐——先上传一首
              </p>
            )}
            {assets.map((a) => (
              <button
                key={a.asset_id}
                onClick={() => void handlePick(a)}
                disabled={a.status !== 'ready' || busyAssetId === a.asset_id}
                className={cn(
                  'flex w-full items-center gap-3 rounded-md border px-3 py-2 text-left text-xs transition-colors',
                  a.status === 'ready'
                    ? 'border-border bg-background/40 hover:border-primary hover:bg-primary/5'
                    : 'cursor-not-allowed border-amber-300/40 bg-amber-50/30 dark:bg-amber-950/20',
                )}
              >
                <span className="flex-1 truncate font-medium">{a.title || a.file_name}</span>
                <span className="font-mono text-[10px] text-muted-foreground">
                  {typeof a.metadata.duration_seconds === 'number'
                    ? `${(a.metadata.duration_seconds as number).toFixed(1)}s`
                    : '?s'}
                  {typeof a.metadata.peak_at_seconds === 'number'
                    ? ` · peak ${(a.metadata.peak_at_seconds as number).toFixed(1)}s`
                    : ''}
                </span>
                <span
                  className={cn(
                    'rounded px-1.5 py-0.5 text-[10px] font-medium',
                    a.status === 'ready'
                      ? 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
                      : a.status === 'processing'
                        ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300'
                        : 'bg-rose-500/15 text-rose-700 dark:text-rose-300',
                  )}
                >
                  {a.status === 'ready' ? '就绪' : a.status === 'processing' ? '分析中' : '失败'}
                </span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
