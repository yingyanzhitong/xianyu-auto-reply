/** 指定买家与商品的 AI 回复禁用规则管理。 */
import { useEffect, useState } from 'react'
import { Ban, Loader2, Trash2, X } from 'lucide-react'
import {
  createAccountAiReplyBlock,
  deleteAccountAiReplyBlock,
  getAccountAiReplyBlocks,
  type AiReplyBlockItem,
} from '@/api/accounts'
import { getApiErrorMessage } from '@/utils/request'
import { useUIStore } from '@/store/uiStore'

interface Props {
  accountId: string
  accountDisplayId: string
  onClose: () => void
}

export function AiReplyBlocksModal({ accountId, accountDisplayId, onClose }: Props) {
  const { addToast } = useUIStore()
  const [blocks, setBlocks] = useState<AiReplyBlockItem[]>([])
  const [buyerId, setBuyerId] = useState('')
  const [itemId, setItemId] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [deletingId, setDeletingId] = useState<number | null>(null)

  const loadBlocks = async () => {
    setLoading(true)
    try {
      const result = await getAccountAiReplyBlocks(accountId)
      if (!result.success) {
        addToast({ type: 'error', message: result.message || '加载禁止AI回复规则失败' })
        return
      }
      setBlocks(result.data || [])
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '加载禁止AI回复规则失败') })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void loadBlocks()
  }, [accountId])

  const handleAdd = async () => {
    const normalizedBuyerId = buyerId.trim()
    const normalizedItemId = itemId.trim()
    if (!normalizedBuyerId || !normalizedItemId) {
      addToast({ type: 'warning', message: '请同时填写买家ID和商品ID' })
      return
    }

    setSaving(true)
    try {
      const result = await createAccountAiReplyBlock(accountId, normalizedBuyerId, normalizedItemId)
      if (!result.success) {
        addToast({ type: 'error', message: result.message || '添加禁止AI回复规则失败' })
        return
      }
      setBuyerId('')
      setItemId('')
      addToast({ type: 'success', message: result.message || '禁止AI回复规则已添加' })
      await loadBlocks()
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '添加禁止AI回复规则失败') })
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (blockId: number) => {
    setDeletingId(blockId)
    try {
      const result = await deleteAccountAiReplyBlock(accountId, blockId)
      if (!result.success) {
        addToast({ type: 'error', message: result.message || '删除禁止AI回复规则失败' })
        return
      }
      setBlocks(prev => prev.filter(block => block.id !== blockId))
      addToast({ type: 'success', message: result.message || '禁止AI回复规则已删除' })
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '删除禁止AI回复规则失败') })
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <div className="modal-overlay">
      <div className="modal-content max-w-2xl max-h-[90vh] flex flex-col">
        <div className="modal-header">
          <h2 className="modal-title flex items-center gap-2">
            <Ban className="w-4 h-4 text-red-500" />
            禁止AI回复
          </h2>
          <button onClick={onClose} className="modal-close" disabled={saving || deletingId !== null}>
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="modal-body flex-1 overflow-y-auto space-y-4">
          <div className="bg-blue-50 dark:bg-blue-900/20 rounded-lg p-3 text-sm text-blue-700 dark:text-blue-300">
            <p>账号: <span className="font-medium">{accountDisplayId}</span></p>
            <p className="text-xs mt-1 opacity-80">仅当账号ID、买家ID和商品ID同时匹配时，才跳过AI回复；关键词和默认回复不受影响。</p>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-[1fr_1fr_auto] gap-3 items-end">
            <label className="block text-sm text-slate-700 dark:text-slate-300">
              买家ID
              <input
                value={buyerId}
                onChange={(event) => setBuyerId(event.target.value)}
                maxLength={64}
                placeholder="输入买家ID"
                className="input-ios mt-1 w-full"
              />
            </label>
            <label className="block text-sm text-slate-700 dark:text-slate-300">
              商品ID
              <input
                value={itemId}
                onChange={(event) => setItemId(event.target.value)}
                maxLength={64}
                placeholder="输入商品ID"
                className="input-ios mt-1 w-full"
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    void handleAdd()
                  }
                }}
              />
            </label>
            <button type="button" onClick={() => void handleAdd()} disabled={saving} className="btn-ios-primary h-10 disabled:opacity-50">
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : '添加'}
            </button>
          </div>

          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="w-6 h-6 animate-spin text-blue-500" />
              <span className="ml-2 text-sm text-slate-500">加载规则中...</span>
            </div>
          ) : blocks.length === 0 ? (
            <p className="py-10 text-center text-sm text-slate-400">暂无精确禁止AI回复规则</p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-700">
              <table className="w-full text-sm">
                <thead className="bg-slate-50 dark:bg-slate-800 text-left text-slate-500 dark:text-slate-400">
                  <tr>
                    <th className="px-3 py-2 font-medium">买家ID</th>
                    <th className="px-3 py-2 font-medium">商品ID</th>
                    <th className="px-3 py-2 font-medium">创建时间</th>
                    <th className="px-3 py-2 font-medium text-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {blocks.map((block) => (
                    <tr key={block.id} className="border-t border-slate-200 dark:border-slate-700">
                      <td className="px-3 py-2 font-mono text-xs">{block.buyer_id}</td>
                      <td className="px-3 py-2 font-mono text-xs">{block.item_id}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-xs text-slate-500">{block.created_at || '-'}</td>
                      <td className="px-3 py-2 text-right">
                        <button
                          type="button"
                          onClick={() => void handleDelete(block.id)}
                          disabled={deletingId !== null}
                          className="inline-flex items-center gap-1 text-xs text-red-500 hover:text-red-600 disabled:opacity-40"
                          title="删除规则"
                        >
                          {deletingId === block.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                          删除
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="modal-footer">
          <button type="button" onClick={onClose} className="btn-ios-secondary" disabled={saving || deletingId !== null}>关闭</button>
        </div>
      </div>
    </div>
  )
}
