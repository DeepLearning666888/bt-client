/** 状态 → 中文文案。
 *
 * 单独放一个文件：`api.ts` 是解析层，只该管「字段对不对」，不该管给人看的字。
 * 用 `Record<TorrentState, string>` 而不是 `Partial<>`，是为了让新增状态时
 * TypeScript 直接报错 —— 少翻一处文案比运行时显示英文状态名更容易被发现。
 */

import type { TorrentState } from './api'

export const STATE_LABELS: Record<TorrentState, string> = {
  downloading: '下载中',
  seeding: '做种中',
  paused: '已暂停',
  checking: '校验中',
  metadata: '获取元数据中',
  finished: '已完成',
  queued: '排队中',
  allocating: '分配空间中',
  pending: '准备中',
  other: '其他状态',
  unknown: '未知状态',
}
