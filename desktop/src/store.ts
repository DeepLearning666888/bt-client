/** 模块级外部 store。
 *
 * 不用 Context：Context 值一变，所有消费者重渲。这里把「快照」和「连接状态」分成
 * 两套订阅，组件只订阅自己关心的那一份，配合 useSyncExternalStore 使用。
 */

import type { Snapshot } from './api'

export type ConnectionStatus = 'connecting' | 'online' | 'offline'

export interface Connection {
  status: ConnectionStatus
  detail: string | null
}

export class EngineStore {
  private snapshot: Snapshot | null = null
  private connection: Connection = { status: 'connecting', detail: null }
  private snapshotListeners = new Set<() => void>()
  private connectionListeners = new Set<() => void>()

  subscribeSnapshot = (listener: () => void): (() => void) => {
    this.snapshotListeners.add(listener)
    return () => {
      this.snapshotListeners.delete(listener)
    }
  }

  getSnapshot = (): Snapshot | null => this.snapshot

  subscribeConnection = (listener: () => void): (() => void) => {
    this.connectionListeners.add(listener)
    return () => {
      this.connectionListeners.delete(listener)
    }
  }

  getConnection = (): Connection => this.connection

  applySnapshot(next: Snapshot): void {
    // 同一份对象不重复广播，避免无意义的重渲
    if (next === this.snapshot) return
    this.snapshot = next
    this.setConnection({ status: 'online', detail: null })
    for (const listener of this.snapshotListeners) listener()
  }

  markOffline(detail: string): void {
    this.setConnection({ status: 'offline', detail })
  }

  markConnecting(): void {
    this.setConnection({ status: 'connecting', detail: null })
  }

  private setConnection(next: Connection): void {
    if (
      next.status === this.connection.status &&
      next.detail === this.connection.detail
    ) {
      return
    }
    this.connection = next
    for (const listener of this.connectionListeners) listener()
  }
}
