/** 在途动作登记：按「发起模块 + 记录 + 动作」认领请求，回包只接回自己的模块。 */

export type InflightKey = {
  module: string
  id: number
  action: string
}

export type ActionReply = {
  ok: boolean
  message: string
  module?: string | null
  action?: string | null
  token?: string | null
  origin_module?: string | null
}

function keyOf({ module, id, action }: InflightKey): string {
  return `${module}#${id}#${action}`
}

/**
 * 在途请求认领表。
 *
 * 页面在发起动作时先 `claim`，回包到达后用 `settle` 核对：
 * 只有回包携带的模块/动作与认领一致才允许结算；别的模块（同号记录）晚到的
 * 结果不会关闭当前模块的提交，也不会把回包写进当前模块的行。
 */
export class InflightRegistry {
  private readonly claims = new Map<string, InflightKey>()

  claim(key: InflightKey): string {
    const token = keyOf(key)
    this.claims.set(token, { ...key })
    return token
  }

  has(token: string): boolean {
    return this.claims.has(token)
  }

  /**
   * 结算一次认领：回包归属不匹配（或未声明归属）时返回 false，
   * 由调用方忽略这次晚到的结果，提交状态保持不变。
   */
  settle(token: string, reply: ActionReply): boolean {
    const claimed = this.claims.get(token)
    if (!claimed) return false
    const replyModule = reply.module ?? reply.origin_module ?? null
    if (replyModule && replyModule !== claimed.module) return false
    if (reply.action && reply.action !== claimed.action) return false
    this.claims.delete(token)
    return true
  }

  release(token: string): void {
    this.claims.delete(token)
  }

  /** 当前仍未结算的认领，供测试与页面状态展示。 */
  pending(module: string): InflightKey[] {
    return [...this.claims.values()].filter((item) => item.module === module)
  }
}

export const inflight = new InflightRegistry()
