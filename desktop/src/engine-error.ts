/** 引擎错误体解析。`{"error":{"code":"...","message":"..."}}` → message。
 *
 * 单独一个文件是因为添加与任务操作两处都要用：结构不对时必须返回 null，
 * 绝不让 undefined 渗进给人看的文案里。这条规则只该写一遍。
 */

export function engineMessage(body: unknown): string | null {
  if (typeof body !== 'object' || body === null) return null
  const error = (body as Record<string, unknown>)['error']
  if (typeof error !== 'object' || error === null) return null
  const message = (error as Record<string, unknown>)['message']
  return typeof message === 'string' && message.trim() !== '' ? message.trim() : null
}
