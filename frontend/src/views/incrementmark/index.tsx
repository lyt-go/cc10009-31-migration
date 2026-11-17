import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/incrementmark'
const columns = [
  "水位编号",
  "目标源",
  "增量字段名",
  "当前水位值",
  "水位类型",
  "水位版本",
  "迁移状态",
  "原字符串水位",
  "迁移版本号",
  "迁移时间"
]
const actions = [
  "标记水位",
  "确认水位",
  "标记过期"
]
const statuses = [
  "草稿",
  "标记中",
  "已确认",
  "已过期"
]
const migrationStatuses = [
  "未迁移",
  "迁移中",
  "已迁移",
  "待人工处理",
  "迁移失败",
  "无需迁移"
]
const manualTypes = ["整数", "小数", "日期", "日期时间", "字符串"]

type Row = Record<string, string | number | null | boolean | Record<string, unknown>>

type MigrationSummary = {
  summary?: { migrated: number; manual: number; failures: number; skipped: number }
  manual?: { 水位编号?: string; 待人工处理原因?: string }[]
  preflight?: { ok: boolean; checks: { name: string; detail: string; ok: boolean }[] }
  message?: string
}

type AuditItem = {
  id: number
  水位编号: string
  目标源: string
  采集顺序: number
  原字符串水位: string
  转换后水位: string
  水位类型: string
  迁移版本号: string
  迁移时间: string
  快照冲突?: boolean
}

type SnapshotVersion = {
  id: number
  目标源: string
  来源版本: string
  原字符串水位: string
  转换后水位: string | null
  采集顺序: number
  迁移时间: string
}

type ManifestItem = {
  id: number
  水位编号: string
  目标源: string
  当前水位值: string
  水位类型: string
  水位版本: string
  只读: boolean
  迁移状态?: string
}

type ManualItem = {
  id: number
  水位编号: string
  目标源: string
  原字符串水位: string
  待人工处理原因: string
}

const stats = [
  { label: '增量水位总数', value: 0 },
  { label: '已迁移水位', value: 0 },
  { label: '待人工处理', value: 0 },
  { label: '只读续采（旧版）', value: 0 }
]

type Tab = 'list' | 'migration' | 'audit' | 'manifest'

export default function IncrementmarkView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [tab, setTab] = useState<Tab>('list')
  const [manualItems, setManualItems] = useState<ManualItem[]>([])
  const [auditItems, setAuditItems] = useState<AuditItem[]>([])
  const [conflicts, setConflicts] = useState<{ 目标源: string; 采用版本: string; 说明: string }[]>([])
  const [snapshotVersions, setSnapshotVersions] = useState<SnapshotVersion[]>([])
  const [manifestItems, setManifestItems] = useState<ManifestItem[]>([])
  const [legacyReadonly, setLegacyReadonly] = useState<ManifestItem[]>([])
  const [preflight, setPreflight] = useState<MigrationSummary['preflight']>()
  const [summary, setSummary] = useState<MigrationSummary['summary']>()
  const [manualTypes_, setManualTypes_] = useState<Record<number, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 2), [])

  const statsValues = useMemo(
    () => [
      total,
      rows.filter((row) => row['迁移状态'] === '已迁移').length,
      manualItems.length,
      legacyReadonly.length
    ],
    [total, rows, manualItems, legacyReadonly]
  )

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('增量水位列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '增量标记列表读取失败')
    }
  }, [filters])

  const reloadPanels = useCallback(async () => {
    try {
      const [manualResponse, auditResponse, snapshotResponse, manifestResponse, preflightResponse] =
        await Promise.all([
          request(`${ENDPOINT}/migration/manual-queue`),
          request(`${ENDPOINT}/migration/audit`),
          request(`${ENDPOINT}/migration/snapshots`),
          request(`${ENDPOINT}/migration/crawl-manifest`),
          request(`${ENDPOINT}/migration/preflight`)
        ])
      const manualPayload = await manualResponse.json()
      const auditPayload = await auditResponse.json()
      const snapshotPayload = await snapshotResponse.json()
      const manifestPayload = await manifestResponse.json()
      const preflightPayload = await preflightResponse.json()
      setManualItems(manualPayload.items ?? [])
      setAuditItems(auditPayload.items ?? [])
      setConflicts(auditPayload.conflicts ?? [])
      setSnapshotVersions(snapshotPayload.versions ?? [])
      setManifestItems(manifestPayload.items ?? [])
      setLegacyReadonly(manifestPayload.legacy_readonly ?? [])
      setPreflight(preflightPayload)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '迁移信息读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void reloadPanels()
  }, [reloadPanels])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } })
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '增量标记动作未生效，请稍后重试')
      }
      setNotice(payload.message ?? '')
      await Promise.all([reload(), reloadPanels()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '增量标记操作失败')
    }
  }

  async function migrate(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/migration`, {
        method: 'POST',
        body: JSON.stringify({ values: {} })
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '水位迁移未完成')
      }
      setNotice(payload.message ?? '')
      await Promise.all([reload(), reloadPanels()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '水位迁移失败')
    }
  }

  async function migrateAll(dryRun: boolean) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/migration/run`, {
        method: 'POST',
        body: JSON.stringify({ values: { dry_run: dryRun } })
      })
      const payload = await response.json()
      const result = payload.entry as MigrationSummary | undefined
      setSummary(result?.summary)
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '批量迁移未完成')
      }
      setNotice(payload.message ?? '')
      await Promise.all([reload(), reloadPanels()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '批量迁移失败')
    }
  }

  async function confirmManual(item: ManualItem) {
    const valueType = manualTypes_[item.id] ?? ''
    if (!valueType) {
      setErrorMessage('请先为该水位选择明确类型，不能猜测后推进')
      return
    }
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${item.id}/manual-confirm`, {
        method: 'POST',
        body: JSON.stringify({ values: { 水位类型: valueType } })
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '人工确认未生效')
      }
      setNotice(payload.message ?? '')
      await Promise.all([reload(), reloadPanels()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '人工确认失败')
    }
  }

  async function syncCrawl() {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/migration/crawl-sync`, {
        method: 'POST',
        body: JSON.stringify({ values: { operator: '值班管理员' } })
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '续采清单回写失败')
      }
      setNotice(payload.message ?? '')
      await Promise.all([reload(), reloadPanels()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '续采清单回写失败')
    }
  }

  return (
    <section className="page" data-module="incrementmark">
      <header className="page-head">
        <div>
          <h2>增量标记管理</h2>
          <p className="page-desc">管理各数据源的增量采集水位标记，支撑断点续采和增量更新；迁移旧版字符串水位到带类型与回退快照的新版格式。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => void migrateAll(true)}>迁移预览</button>
          <button className="btn primary" type="button" onClick={() => void migrateAll(false)}>执行迁移</button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出增量标记清单
          </button>
        </div>
      </header>

      <div className="tab-row">
        {([
          ['list', '水位清单'],
          ['migration', '迁移与人工处理'],
          ['audit', '审计与快照'],
          ['manifest', '采集执行侧清单']
        ] as [Tab, string][]).map(([key, label]) => (
          <button
            key={key}
            type="button"
            className={tab === key ? 'tab active' : 'tab'}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="stat-row">
        {stats.map((item, index) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{statsValues[index]}</strong>
          </article>
        ))}
      </div>

      {notice && <p className="notice-text">{notice}</p>}

      {tab === 'list' && (
        <>
          <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
            {filterFields.map((field) => (
              <label key={field} className="filter-item">
                <span>{field}</span>
                <input
                  value={filters[field] ?? ''}
                  placeholder={`按${field}检索`}
                  onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
                />
              </label>
            ))}
            <label className="filter-item">
              <span>迁移状态</span>
              <select
                value={filters.migration_status ?? ''}
                onChange={(event) => setFilters((prev) => ({ ...prev, migration_status: event.target.value }))}
              >
                <option value="">全部</option>
                {migrationStatuses.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </label>
            <label className="filter-item">
              <span>水位状态</span>
              <select
                value={filters.status ?? ''}
                onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
              >
                <option value="">全部</option>
                {statuses.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </label>
            <button className="btn" type="submit">查询</button>
            <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
          </form>

          <table className="data-table">
            <thead>
              <tr>
                {columns.map((column) => <th key={column}>{column}</th>)}
                <th>可执行动作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={String(row.id)}>
                  {columns.map((column) => <td key={column}>{String(row[column] ?? '—')}</td>)}
                  <td className="row-actions">
                    {actions.map((action) => (
                      <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                        {action}
                      </button>
                    ))}
                    {row['迁移状态'] !== '已迁移' && (
                      <button className="link" type="button" onClick={() => void migrate(row)}>
                        迁移水位
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td className="empty-state" colSpan={columns.length + 1}>暂无增量标记数据，可先登记增量水位</td>
                </tr>
              )}
            </tbody>
          </table>
        </>
      )}

      {tab === 'migration' && (
        <>
          <section className="panel">
            <h3>迁移前核对</h3>
            {preflight?.checks ? (
              <ul className="check-list">
                {preflight.checks.map((check) => (
                  <li key={check.name} className={check.ok ? 'ok' : 'bad'}>
                    <strong>{check.name}</strong>：{check.detail}
                  </li>
                ))}
              </ul>
            ) : <p className="empty-state">核对结果读取中</p>}
            {summary && (
              <p className="page-desc">
                本轮：迁移 {summary.migrated} 条，待人工 {summary.manual} 条，失败 {summary.failures} 条，跳过 {summary.skipped} 条。
                已迁移条目重复迁移不会生成第二个版本。
              </p>
            )}
          </section>

          <section className="panel">
            <h3>待人工处理清单</h3>
            <p className="page-desc">类型无法识别的水位停在这里，必须指定明确类型后才能推进，系统不做猜测。</p>
            <table className="data-table">
              <thead>
                <tr><th>水位编号</th><th>目标源</th><th>原字符串水位</th><th>待人工处理原因</th><th>指定类型</th><th>操作</th></tr>
              </thead>
              <tbody>
                {manualItems.map((item) => (
                  <tr key={item.id}>
                    <td>{item.水位编号}</td>
                    <td>{item.目标源}</td>
                    <td>{item.原字符串水位}</td>
                    <td>{item.待人工处理原因}</td>
                    <td>
                      <select
                        value={manualTypes_[item.id] ?? ''}
                        onChange={(event) => setManualTypes_((prev) => ({ ...prev, [item.id]: event.target.value }))}
                      >
                        <option value="">请选择</option>
                        {manualTypes.map((type) => <option key={type} value={type}>{type}</option>)}
                      </select>
                    </td>
                    <td>
                      <button className="link" type="button" onClick={() => void confirmManual(item)}>确认并迁移</button>
                    </td>
                  </tr>
                ))}
                {manualItems.length === 0 && (
                  <tr><td className="empty-state" colSpan={6}>没有待人工处理的水位</td></tr>
                )}
              </tbody>
            </table>
          </section>
        </>
      )}

      {tab === 'audit' && (
        <>
          <section className="panel">
            <h3>迁移审计（逐项对照输入）</h3>
            <table className="data-table">
              <thead>
                <tr>
                  <th>采集顺序</th><th>水位编号</th><th>目标源</th><th>原字符串水位</th>
                  <th>转换后水位</th><th>水位类型</th><th>迁移版本号</th><th>迁移时间</th><th>快照冲突</th>
                </tr>
              </thead>
              <tbody>
                {auditItems.map((item) => (
                  <tr key={item.id}>
                    <td>{item.采集顺序}</td>
                    <td>{item.水位编号}</td>
                    <td>{item.目标源}</td>
                    <td>{item.原字符串水位}</td>
                    <td>{item.转换后水位}</td>
                    <td>{item.水位类型}</td>
                    <td>{item.迁移版本号}</td>
                    <td>{item.迁移时间}</td>
                    <td>{item.快照冲突 ? '是' : '—'}</td>
                  </tr>
                ))}
                {auditItems.length === 0 && (
                  <tr><td className="empty-state" colSpan={9}>还没有已迁移的水位</td></tr>
                )}
              </tbody>
            </table>
          </section>

          {conflicts.length > 0 && (
            <section className="panel">
              <h3>同一源的新旧版本先后关系</h3>
              {conflicts.map((conflict) => (
                <p key={conflict.目标源} className="page-desc">
                  目标源 {conflict.目标源}：{conflict.说明}，采用版本 {conflict.采用版本}。
                </p>
              ))}
            </section>
          )}

          <section className="panel">
            <h3>可追溯水位快照（两个版本）</h3>
            <table className="data-table">
              <thead>
                <tr><th>目标源</th><th>版本</th><th>原字符串水位</th><th>转换后水位</th><th>采集顺序</th><th>迁移时间</th></tr>
              </thead>
              <tbody>
                {snapshotVersions.map((item, index) => (
                  <tr key={`${item.id}-${item.来源版本}-${index}`}>
                    <td>{item.目标源}</td>
                    <td>{item.来源版本}</td>
                    <td>{item.原字符串水位}</td>
                    <td>{item.转换后水位 ?? '—'}</td>
                    <td>{item.采集顺序}</td>
                    <td>{item.迁移时间 ?? '—'}</td>
                  </tr>
                ))}
                {snapshotVersions.length === 0 && (
                  <tr><td className="empty-state" colSpan={6}>还没有可追溯水位快照</td></tr>
                )}
              </tbody>
            </table>
          </section>
        </>
      )}

      {tab === 'manifest' && (
        <>
          <section className="panel">
            <h3>采集执行侧清单</h3>
            <p className="page-desc">已迁移水位按新版带类型消费；未迁移水位仍以原字符串只读续采。</p>
            <button className="btn" type="button" onClick={() => void syncCrawl()}>回写采集执行侧清单</button>
            <table className="data-table">
              <thead>
                <tr><th>水位编号</th><th>目标源</th><th>当前水位值</th><th>水位类型</th><th>水位版本</th><th>续采方式</th></tr>
              </thead>
              <tbody>
                {[...manifestItems, ...legacyReadonly].map((item) => (
                  <tr key={String(item.id)}>
                    <td>{item.水位编号}</td>
                    <td>{item.目标源}</td>
                    <td>{item.当前水位值}</td>
                    <td>{item.水位类型}</td>
                    <td>{item.水位版本}</td>
                    <td>{item.只读 ? '只读续采（旧版）' : '新版续采'}</td>
                  </tr>
                ))}
                {manifestItems.length + legacyReadonly.length === 0 && (
                  <tr><td className="empty-state" colSpan={6}>采集执行侧清单为空</td></tr>
                )}
              </tbody>
            </table>
          </section>
        </>
      )}

      <footer className="page-foot">
        <span>共 {total} 条增量标记记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
