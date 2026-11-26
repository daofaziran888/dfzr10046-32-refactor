import { describe, expect, it } from 'vitest'

import { InflightRegistry } from '@/api/inflight'

describe('InflightRegistry', () => {
  it('同号记录分属两页时各认各的', () => {
    const registry = new InflightRegistry()
    const reprocess = registry.claim({ module: 'reprocess_task', id: 2, action: '排期重跑' })
    const lineage = registry.claim({ module: 'lineage_edge', id: 2, action: '标记失效' })

    expect(reprocess).not.toBe(lineage)
    expect(registry.pending('reprocess_task')).toHaveLength(1)
    expect(registry.pending('lineage_edge')).toHaveLength(1)
  })

  it('晚到的回包不会关闭另一模块的提交', () => {
    const registry = new InflightRegistry()
    const reprocess = registry.claim({ module: 'reprocess_task', id: 2, action: '排期重跑' })
    const lineage = registry.claim({ module: 'lineage_edge', id: 2, action: '标记失效' })

    // 血缘回包先到，用重跑的 token 结算必须失败，重跑仍在途。
    const accepted = registry.settle(reprocess, {
      ok: true,
      message: '血缘边已标记失效',
      module: 'lineage_edge',
      action: '标记失效',
    })
    expect(accepted).toBe(false)
    expect(registry.pending('reprocess_task')).toHaveLength(1)

    // 血缘自己的 token 才能结算血缘的认领。
    expect(
      registry.settle(lineage, {
        ok: true,
        message: '血缘边已标记失效',
        module: 'lineage_edge',
        action: '标记失效',
      }),
    ).toBe(true)
    expect(registry.pending('lineage_edge')).toHaveLength(0)
  })

  it('动作与认领不一致的回包也不结算', () => {
    const registry = new InflightRegistry()
    const token = registry.claim({ module: 'reprocess_task', id: 2, action: '排期重跑' })
    expect(
      registry.settle(token, {
        ok: true,
        message: '重跑任务已确认完成',
        module: 'reprocess_task',
        action: '确认完成',
      }),
    ).toBe(false)
    expect(registry.has(token)).toBe(true)
  })

  it('没有归属信息的回包按当前认领处理，兼容旧后端', () => {
    const registry = new InflightRegistry()
    const token = registry.claim({ module: 'lineage_edge', id: 4, action: '确认核对' })
    expect(registry.settle(token, { ok: true, message: '血缘边已确认核对' })).toBe(true)
  })
})
