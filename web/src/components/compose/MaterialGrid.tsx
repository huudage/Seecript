import {
  DndContext,
  type DragEndEvent,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
} from '@dnd-kit/core'
import { SortableContext, rectSortingStrategy, useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'

import { MaterialCard } from './MaterialCard'
import type { Material } from '@/types/schemas'

/**
 * dnd-kit 拖拽排序网格。
 * - 上层把 store.materials 按 sort_order 排好后传进来
 * - onReorder 收到新的 material_id 顺序，调用 store.reorderMaterials
 * - 视觉：3 列网格，移动端 1 列
 */
export function MaterialGrid({
  materials,
  onReorder,
  onRemove,
}: {
  materials: Material[]
  onReorder: (orderedIds: string[]) => void
  onRemove: (id: string) => void
}) {
  // 8px 阈值——避免误触把点击当拖拽
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 8 } }))

  const handleDragEnd = (e: DragEndEvent) => {
    const { active, over } = e
    if (!over || active.id === over.id) return
    const oldIndex = materials.findIndex((m) => m.material_id === active.id)
    const newIndex = materials.findIndex((m) => m.material_id === over.id)
    if (oldIndex < 0 || newIndex < 0) return
    const next = materials.slice()
    const [moved] = next.splice(oldIndex, 1)
    next.splice(newIndex, 0, moved)
    onReorder(next.map((m) => m.material_id))
  }

  if (materials.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border bg-background/30 p-6 text-center text-xs text-muted-foreground">
        还没有素材；拖文件到上方虚线区或点击上传。
      </div>
    )
  }

  return (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
      <SortableContext
        items={materials.map((m) => m.material_id)}
        strategy={rectSortingStrategy}
      >
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {materials.map((m) => (
            <SortableItem key={m.material_id} material={m} onRemove={onRemove} />
          ))}
        </div>
      </SortableContext>
    </DndContext>
  )
}

function SortableItem({ material, onRemove }: { material: Material; onRemove: (id: string) => void }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: material.material_id,
  })
  return (
    <div
      ref={setNodeRef}
      style={{
        transform: CSS.Transform.toString(transform),
        transition,
        opacity: isDragging ? 0.5 : 1,
        zIndex: isDragging ? 10 : 'auto',
      }}
    >
      <MaterialCard
        material={material}
        dragHandleProps={{ ...attributes, ...listeners }}
        onRemove={onRemove}
      />
    </div>
  )
}
