/** 鱼小铺独立批量发布页面。 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import { AlertTriangle, CheckCircle, Clock, Layers, Loader2, Play, XCircle } from 'lucide-react'
import { getAccountDetails } from '@/api/accounts'
import {
  getMaterials,
  getShopBatchStatus,
  publishShopBatch,
  type ProductMaterial,
  type ShopBatchStatusResponse,
} from '@/api/productPublish'
import { useUIStore } from '@/store/uiStore'

type ShopBatchProgress = ShopBatchStatusResponse['data']

const BATCH_ID_STORAGE_KEY = 'shop_batch_publish_active_batch_id'
const SHIPPING_LABELS: Record<string, string> = {
  free: '包邮',
  distance: '按距离计费',
  fixed: '一口价',
  no_shipping: '无需邮寄',
}

function getMaterialShopLabel(material: ProductMaterial) {
  const stock = material.shop_stock ?? 999
  const shipping = material.shop_shipping_mode ? SHIPPING_LABELS[material.shop_shipping_mode] : '包邮（默认）'
  const hasFansPrice = [material.shop_fans_price_all, material.shop_fans_price_old, material.shop_fans_price_bought]
    .some(value => value != null)
  return `库存 ${stock} · ${shipping}${hasFansPrice ? ' · 已配置粉丝价' : ''}`
}

export function ShopBatchPublish() {
  const { addToast } = useUIStore()
  const [accounts, setAccounts] = useState<any[]>([])
  const [materials, setMaterials] = useState<ProductMaterial[]>([])
  const [selectedAccounts, setSelectedAccounts] = useState<Set<string>>(new Set())
  const [selectedMaterials, setSelectedMaterials] = useState<Set<number>>(new Set())
  const [loadingAccounts, setLoadingAccounts] = useState(true)
  const [loadingMaterials, setLoadingMaterials] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [progress, setProgress] = useState<ShopBatchProgress | null>(null)
  const [materialSearch, setMaterialSearch] = useState('')
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const clearStoredBatchId = useCallback(() => {
    try { sessionStorage.removeItem(BATCH_ID_STORAGE_KEY) } catch { /* ignore */ }
  }, [])

  const storeBatchId = useCallback((batchId: string) => {
    try { sessionStorage.setItem(BATCH_ID_STORAGE_KEY, batchId) } catch { /* ignore */ }
  }, [])

  const startPolling = useCallback((batchId: string) => {
    if (pollingRef.current) clearInterval(pollingRef.current)
    pollingRef.current = setInterval(async () => {
      try {
        const res = await getShopBatchStatus(batchId)
        if (!res.success) {
          if (pollingRef.current) clearInterval(pollingRef.current)
          clearStoredBatchId()
          return
        }
        setProgress(res.data)
        if (res.data.finished) {
          if (pollingRef.current) clearInterval(pollingRef.current)
          clearStoredBatchId()
          addToast({
            type: res.data.failed === 0 && res.data.warning_count === 0 ? 'success' : 'warning',
            message: `鱼小铺批量发布完成：成功 ${res.data.success} 条，失败 ${res.data.failed} 条${res.data.warning_count ? `，告警 ${res.data.warning_count} 条` : ''}`,
          })
        }
      } catch { /* 轮询失败时等待下一次 */ }
    }, 3000)
  }, [addToast, clearStoredBatchId])

  useEffect(() => {
    getAccountDetails()
      .then(list => setAccounts(list))
      .catch(() => addToast({ type: 'error', message: '账号加载失败' }))
      .finally(() => setLoadingAccounts(false))
    getMaterials(1, 1000)
      .then(res => {
        if (res.success) setMaterials(res.data.list)
        else addToast({ type: 'error', message: res.message || '素材加载失败' })
      })
      .catch(() => addToast({ type: 'error', message: '素材加载失败' }))
      .finally(() => setLoadingMaterials(false))

    try {
      const batchId = sessionStorage.getItem(BATCH_ID_STORAGE_KEY)
      if (batchId) {
        getShopBatchStatus(batchId).then(res => {
          if (!res.success) {
            clearStoredBatchId()
            return
          }
          setProgress(res.data)
          if (!res.data.finished) startPolling(batchId)
          else clearStoredBatchId()
        }).catch(() => { /* 保留任务键，稍后重试 */ })
      }
    } catch { /* ignore */ }
    return () => { if (pollingRef.current) clearInterval(pollingRef.current) }
  }, [addToast, clearStoredBatchId, startPolling])

  const filteredMaterials = materialSearch.trim()
    ? materials.filter(material => material.title.toLowerCase().includes(materialSearch.trim().toLowerCase()))
    : materials
  const total = selectedAccounts.size * selectedMaterials.size
  const isDisabled = submitting || total === 0 || Boolean(progress && !progress.finished)

  const toggleAccount = (accountId: string) => setSelectedAccounts(previous => {
    const next = new Set(previous)
    next.has(accountId) ? next.delete(accountId) : next.add(accountId)
    return next
  })
  const toggleMaterial = (materialId: number) => setSelectedMaterials(previous => {
    const next = new Set(previous)
    next.has(materialId) ? next.delete(materialId) : next.add(materialId)
    return next
  })
  const toggleAllAccounts = () => setSelectedAccounts(previous =>
    previous.size === accounts.length ? new Set() : new Set(accounts.map((account: any) => account.id))
  )
  const toggleAllMaterials = () => {
    const ids = filteredMaterials.map(material => material.id)
    const allSelected = ids.length > 0 && ids.every(id => selectedMaterials.has(id))
    setSelectedMaterials(previous => {
      const next = new Set(previous)
      ids.forEach(id => allSelected ? next.delete(id) : next.add(id))
      return next
    })
  }

  const handleSubmit = async () => {
    if (selectedAccounts.size === 0) { addToast({ type: 'warning', message: '请至少选择一个账号' }); return }
    if (selectedMaterials.size === 0) { addToast({ type: 'warning', message: '请至少选择一条素材' }); return }
    setSubmitting(true)
    try {
      const res = await publishShopBatch({
        account_ids: Array.from(selectedAccounts),
        material_ids: Array.from(selectedMaterials),
      })
      if (!res.success || !res.data?.batch_id) {
        addToast({ type: 'error', message: res.message || '鱼小铺任务提交失败' })
        return
      }
      const batchId = res.data.batch_id
      storeBatchId(batchId)
      setProgress({
        batch_id: batchId,
        total: res.data.total ?? total,
        success: 0,
        failed: 0,
        warning_count: 0,
        publishing: 0,
        pending: res.data.total ?? total,
        status: 'queued',
        finished: false,
        items: [],
      })
      addToast({ type: 'success', message: res.message || '鱼小铺批量发布任务已提交' })
      startPolling(batchId)
    } catch {
      addToast({ type: 'error', message: '网络错误，请重试' })
    } finally {
      setSubmitting(false)
    }
  }

  const warningItems = progress?.items.filter(item => item.warning_message) ?? []
  const failureItems = progress?.items.filter(item => item.status === 'failed') ?? []
  const completedCount = progress ? progress.success + progress.failed : 0

  return (
    <div className="space-y-3 sm:space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <h1 className="page-title">鱼小铺批量发布</h1>
          <p className="page-description">使用鱼小铺卖家发布页，支持库存、发货设置和粉丝价</p>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">宝贝所在地仍由随机地址库分配；粉丝价设置失败不会重复发布商品。</p>
        </div>
        <div className="text-sm text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-950/30 px-3 py-1.5 rounded-lg">
          {selectedAccounts.size} 账号 × {selectedMaterials.size} 素材 =&nbsp;
          <span className="font-semibold">{total} 次发布</span>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} className="vben-card">
          <div className="vben-card-header">
            <h2 className="vben-card-title">选择账号</h2>
            <button className="text-sm text-blue-500 hover:underline" onClick={toggleAllAccounts}>
              {selectedAccounts.size === accounts.length && accounts.length > 0 ? '取消全选' : '全选'}
            </button>
          </div>
          <div className="vben-card-body">
            {loadingAccounts ? <div className="flex justify-center py-8"><Loader2 className="w-8 h-8 animate-spin text-blue-500" /></div>
              : accounts.length === 0 ? <p className="text-center text-slate-400 py-8">暂无账号，请先添加账号</p>
                : <div className="space-y-1 max-h-72 overflow-y-auto">{accounts.map((account: any) => {
                  const checked = selectedAccounts.has(account.id)
                  return <label key={account.id} className={`flex items-center gap-3 p-2.5 rounded-lg cursor-pointer transition-colors ${checked ? 'bg-amber-50 dark:bg-amber-950/20' : 'hover:bg-slate-50 dark:hover:bg-slate-700'}`}>
                    <input type="checkbox" className="w-4 h-4 accent-amber-500" checked={checked} onChange={() => toggleAccount(account.id)} />
                    <div className="flex-1 min-w-0"><p className="text-sm font-medium truncate text-slate-800 dark:text-slate-100">{account.note || account.id}</p>{account.note && <p className="text-xs text-slate-400 truncate">{account.id}</p>}</div>
                    {account.enabled !== false && <span className="badge-success flex-shrink-0">启用</span>}
                  </label>
                })}</div>}
          </div>
        </motion.div>

        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.05 }} className="vben-card">
          <div className="vben-card-header">
            <h2 className="vben-card-title">选择素材</h2>
            <button className="text-sm text-blue-500 hover:underline" onClick={toggleAllMaterials}>
              {selectedMaterials.size === filteredMaterials.length && filteredMaterials.length > 0 ? '取消全选' : '全选'}
            </button>
          </div>
          <div className="vben-card-body">
            <input className="input-ios w-full mb-2" placeholder="搜索素材标题..." value={materialSearch} onChange={event => setMaterialSearch(event.target.value)} />
            {loadingMaterials ? <div className="flex justify-center py-8"><Loader2 className="w-8 h-8 animate-spin text-blue-500" /></div>
              : filteredMaterials.length === 0 ? <p className="text-center text-slate-400 py-8">暂无匹配素材</p>
                : <div className="space-y-1 max-h-72 overflow-y-auto">{filteredMaterials.map(material => {
                  const checked = selectedMaterials.has(material.id)
                  return <label key={material.id} className={`flex items-center gap-3 p-2.5 rounded-lg cursor-pointer transition-colors ${checked ? 'bg-amber-50 dark:bg-amber-950/20' : 'hover:bg-slate-50 dark:hover:bg-slate-700'}`}>
                    <input type="checkbox" className="w-4 h-4 accent-amber-500" checked={checked} onChange={() => toggleMaterial(material.id)} />
                    {material.images?.[0] ? <img src={material.images[0]} alt={material.title} className="w-10 h-10 object-cover rounded-lg flex-shrink-0" /> : <div className="w-10 h-10 bg-slate-100 dark:bg-slate-700 rounded-lg flex items-center justify-center text-xs text-slate-400 flex-shrink-0">无图</div>}
                    <div className="flex-1 min-w-0"><p className="text-sm font-medium truncate text-slate-800 dark:text-slate-100">{material.title}</p><p className="text-xs text-amber-600">¥{material.price} · {getMaterialShopLabel(material)}</p></div>
                  </label>
                })}</div>}
          </div>
        </motion.div>
      </div>

      <div className="flex justify-center">
        <button className="btn-ios-primary min-w-56" disabled={isDisabled} onClick={handleSubmit}>
          {submitting ? <><Loader2 className="w-4 h-4 animate-spin" />提交中...</> : <><Play className="w-4 h-4" />开始鱼小铺批量发布（{total} 次）</>}
        </button>
      </div>

      {progress && <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="vben-card">
        <div className="vben-card-header"><h2 className="vben-card-title"><Layers className="w-4 h-4" />鱼小铺发布进度</h2>{progress.finished ? <span className="badge-success">已完成</span> : <Loader2 className="w-4 h-4 animate-spin text-amber-500" />}</div>
        <div className="vben-card-body">
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 mb-4">
            {[
              { label: '总数', value: progress.total, icon: <Layers className="w-5 h-5" />, cls: 'stat-icon-primary' },
              { label: '成功', value: progress.success, icon: <CheckCircle className="w-5 h-5" />, cls: 'stat-icon-success' },
              { label: '失败', value: progress.failed, icon: <XCircle className="w-5 h-5" />, cls: 'stat-icon-warning' },
              { label: '告警', value: progress.warning_count, icon: <AlertTriangle className="w-5 h-5" />, cls: 'stat-icon-warning' },
              { label: '进行中', value: progress.publishing + progress.pending, icon: <Clock className="w-5 h-5" />, cls: 'stat-icon-info' },
            ].map(stat => <div key={stat.label} className="stat-card"><div className={stat.cls}>{stat.icon}</div><div><div className="stat-value">{stat.value}</div><div className="stat-label">{stat.label}</div></div></div>)}
          </div>
          {progress.total > 0 && <><div className="w-full bg-slate-200 dark:bg-slate-700 rounded-full h-2 mb-1"><div className="bg-amber-500 h-2 rounded-full transition-all duration-500" style={{ width: `${Math.round(completedCount / progress.total * 100)}%` }} /></div><div className="flex justify-between text-xs text-slate-400"><span>进度 {Math.round(completedCount / progress.total * 100)}%</span><span>批次 ID：{progress.batch_id.slice(0, 8)}...</span></div></>}
          {!progress.finished && <p className="text-xs text-slate-400 mt-2">每 3 秒自动刷新进度</p>}
          {failureItems.length > 0 && <div className="mt-4 border-t border-slate-200 dark:border-slate-700 pt-4"><h3 className="text-sm font-semibold text-rose-700 dark:text-rose-300 mb-2">发布失败详情</h3><div className="space-y-2">{failureItems.map(item => <div key={item.log_id} className="rounded-lg bg-rose-50 dark:bg-rose-950/30 px-3 py-2 text-xs text-rose-800 dark:text-rose-200"><span className="font-medium">{item.account_id}{item.title ? ` · ${item.title}` : ''}</span>：{item.error_message || '未返回失败原因，请到发布日志查看'}</div>)}</div></div>}
          {warningItems.length > 0 && <div className="mt-4 border-t border-slate-200 dark:border-slate-700 pt-4"><h3 className="text-sm font-semibold text-amber-700 dark:text-amber-300 mb-2">发布成功但有告警</h3><div className="space-y-2">{warningItems.map(item => <div key={item.log_id} className="rounded-lg bg-amber-50 dark:bg-amber-950/30 px-3 py-2 text-xs text-amber-800 dark:text-amber-200"><span className="font-medium">{item.account_id}</span>：{item.warning_message}</div>)}</div></div>}
        </div>
      </motion.div>}
    </div>
  )
}

export default ShopBatchPublish
