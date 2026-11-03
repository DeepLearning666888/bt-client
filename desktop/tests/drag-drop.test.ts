/** AC-20 / AC-21：拖拽的判定层。
 *
 * 这里不开浏览器 —— dragover / drop 事件对象在 jsdom 里造起来很别扭，
 * 而真正容易写错的是取舍规则：多文件怎么办、什么该报错、什么该闭嘴。
 * 那部分是纯函数，正好能一条一条钉住。
 */

import { describe, expect, it } from 'vitest'
import { hasDraggableContent, planDrop } from '../src/drag-drop'

const MAGNET =
  'magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c&dn=Big+Buck+Bunny'

function drop(over: {
  types?: string[]
  files?: { name: string; size: number }[]
  text?: string
}): ReturnType<typeof planDrop> {
  return planDrop({
    types: over.types ?? [],
    files: over.files ?? [],
    text: over.text ?? '',
  })
}

describe('文件优先于文本', () => {
  it('一个 .torrent 文件走文件路径', () => {
    const plan = drop({ files: [{ name: 'ubuntu.torrent', size: 4096 }] })
    expect(plan.kind).toBe('file')
    expect(plan.kind === 'file' && plan.file.name).toBe('ubuntu.torrent')
  })

  it('拖文件时浏览器会塞文件名进 text/plain，不能被当成磁力文本', () => {
    const plan = drop({
      files: [{ name: 'a.torrent', size: 100 }],
      text: 'a.torrent',
    })
    expect(plan.kind).toBe('file')
  })

  it('文件合格时即便同时带着合法磁力文本，也以文件为准', () => {
    const plan = drop({
      files: [{ name: 'a.torrent', size: 100 }],
      text: MAGNET,
    })
    expect(plan.kind).toBe('file')
  })
})

describe('非法文件给出可读原因', () => {
  it('后缀不对：文案里带文件名，用户才知道是哪一个', () => {
    const plan = drop({ files: [{ name: 'ubuntu.iso', size: 1024 }] })
    expect(plan.kind).toBe('reject')
    expect(plan.kind === 'reject' && plan.message).toContain('ubuntu.iso')
  })

  it('0 字节', () => {
    const plan = drop({ files: [{ name: 'empty.torrent', size: 0 }] })
    expect(plan.kind).toBe('reject')
    expect(plan.kind === 'reject' && plan.message).toContain('0 字节')
  })

  it('超过 8 MiB 时把两个数值都写出来', () => {
    const plan = drop({ files: [{ name: 'big.torrent', size: 9 * 1024 * 1024 }] })
    expect(plan.kind).toBe('reject')
    expect(plan.kind === 'reject' && plan.message).toContain('9.0 MiB')
    expect(plan.kind === 'reject' && plan.message).toContain('8.0 MiB')
  })
})

describe('多文件不静默取第一个', () => {
  it('两个文件直接拒绝，并说明只支持一个', () => {
    const plan = drop({
      files: [
        { name: 'a.torrent', size: 100 },
        { name: 'b.torrent', size: 200 },
      ],
    })
    expect(plan.kind).toBe('reject')
    expect(plan.kind === 'reject' && plan.message).toContain('2 个')
  })

  it('五个文件时把数量写对', () => {
    const files = Array.from({ length: 5 }, (_, i) => ({ name: `${i}.torrent`, size: 10 }))
    const plan = drop({ files })
    expect(plan.kind === 'reject' && plan.message).toContain('5 个')
  })
})

describe('文本路径', () => {
  it('合法磁力走磁力路径', () => {
    const plan = drop({ text: MAGNET })
    expect(plan.kind).toBe('magnet')
    expect(plan.kind === 'magnet' && plan.magnet).toBe(MAGNET)
  })

  it('首尾空白被去掉', () => {
    const plan = drop({ text: `  \n${MAGNET}\t ` })
    expect(plan.kind === 'magnet' && plan.magnet).toBe(MAGNET)
  })

  it('大写 MAGNET: 也认', () => {
    const plan = drop({ text: 'MAGNET:' + MAGNET.slice('magnet:'.length) })
    expect(plan.kind).toBe('magnet')
  })

  it('想当磁力但不完整 → 报错（这条是有意报错的）', () => {
    const plan = drop({ text: 'magnet:?xt=urn:btih:dd8255ec' })
    expect(plan.kind).toBe('reject')
    expect(plan.kind === 'reject' && plan.message).toContain('btih')
  })

  it('magnet: 开头但拖了个网页地址在后面 → 报错', () => {
    const plan = drop({ text: 'magnet:?dn=https://example.com/x.torrent' })
    expect(plan.kind).toBe('reject')
  })
})

describe('无关文本静默忽略', () => {
  it('随手拖一段普通文字进来不弹提示', () => {
    expect(drop({ text: '这是一段随手选中的文字' }).kind).toBe('ignore')
  })

  it('网页地址也不报错（它根本不是想加任务）', () => {
    expect(drop({ text: 'https://example.com/' }).kind).toBe('ignore')
  })

  it('什么都不带（空文本、空文件列表）', () => {
    expect(drop({}).kind).toBe('ignore')
  })

  it('只有空白字符', () => {
    expect(drop({ text: '   \n\t ' }).kind).toBe('ignore')
  })
})

describe('遮罩判定只看类型', () => {
  it('文件', () => {
    expect(hasDraggableContent(['Files'])).toBe(true)
  })

  it('类型大小写不敏感', () => {
    expect(hasDraggableContent(['files'])).toBe(true)
  })

  it('纯文本', () => {
    expect(hasDraggableContent(['text/plain'])).toBe(true)
  })

  it('拖链接时浏览器给 text/uri-list', () => {
    expect(hasDraggableContent(['text/uri-list'])).toBe(true)
  })

  it('选了文本和文件两样时任意一个命中即可', () => {
    expect(hasDraggableContent(['application/x-moz-file', 'text/plain'])).toBe(true)
  })

  it('应用私有类型不算：拖别的软件的内部对象进来不该亮遮罩', () => {
    expect(hasDraggableContent(['application/x-photoshop-layer'])).toBe(false)
  })

  it('空列表（拖拽已结束）', () => {
    expect(hasDraggableContent([])).toBe(false)
  })
})
