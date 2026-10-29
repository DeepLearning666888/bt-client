fn main() {
    // 生成 Tauri 的上下文（读取 tauri.conf.json，嵌入前端资源与图标）。
    // 前端产物不存在时这里会失败 —— 那是正确的失败：壳没有页面可显示。
    tauri_build::build()
}
