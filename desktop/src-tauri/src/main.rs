// release 下用 Windows GUI 子系统，否则双击会先弹一个黑框。
// debug 保留控制台：开发时要能看到 panic 与日志。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    btclient_shell::run()
}
