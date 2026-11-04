//! 壳被强杀后重启，能不能接管留下的孤儿引擎。
//!
//! 场景：用户在任务管理器结束 `BT 客户端.exe`。Windows 上子进程不随父进程退出，
//! `engine.exe` 成了孤儿继续跑，`engine.lock` 还被它占着。
//!
//! AC-02 覆盖的 Respawn 是「端点残留 + 进程已死」，那是干净情形。这里测脏的：
//! 引擎活着、锁被占，壳重启时能不能正确接管。两类要分开：
//!
//! 1. **端点完好，只是这一次探测没响应**（引擎忙）→ 应当用更长超时重探并接管
//! 2. **端点里的地址真的失效**（文件损坏 / 端口丢了）→ 无法自愈，但必须
//!    （a）报出可操作的错误，而不是 `exit code: 2`
//!    （b）把端点文件还原回去，别让下一次启动也失去线索

use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use btclient_shell::engine::{self, Endpoint};

fn engine_exe() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../engine/build/engine.exe")
}

fn fresh_dir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "btclient-takeover-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// 起一个「孤儿」引擎：拿到端点后丢掉 child 句柄。
/// `Child` 的 Drop 不会杀进程，所以引擎继续活着 —— 正是壳被强杀后的状态。
fn orphan(dir: &Path) -> Endpoint {
    let resolved = engine::spawn(&engine_exe(), dir).expect("拉起引擎失败");
    let endpoint = resolved.endpoint.clone();
    drop(resolved.child);
    endpoint
}

fn cleanup(dir: &Path, endpoint: &Endpoint) {
    let _ = engine::shutdown(endpoint);
    std::thread::sleep(Duration::from_millis(600));
    let _ = std::fs::remove_dir_all(dir);
}

/// 会「抖一下」的 TCP 代理：第一个连接收下但不转发（模拟引擎忙到不响应），
/// 之后的连接正常转发。
///
/// 用它才能造出「引擎活着、端点完好、但健康探测失败」—— 这是壳误判 Respawn 的
/// 真实成因，直接桩掉 `health_ok` 会跳过整个 TCP 判定，测的就不是真链路了。
fn flaky_proxy(target_port: u16) -> (u16, Arc<AtomicBool>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let stop = Arc::new(AtomicBool::new(false));
    let stop_for_thread = Arc::clone(&stop);

    std::thread::spawn(move || {
        let mut first = true;
        for incoming in listener.incoming() {
            if stop_for_thread.load(Ordering::Relaxed) {
                break;
            }
            let Ok(mut client) = incoming else { break };
            if first {
                first = false;
                // 不能用 sleep 卡住 accept 循环：那样第二次探测根本连不上来，
                // 测的就成了「代理全程坏」而不是「抖了一下」。挂起顺便接新连接。
                let held = client;
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_millis(2200));
                    drop(held);
                });
                continue;
            }
            std::thread::spawn(move || {
                let Ok(mut upstream) = TcpStream::connect(("127.0.0.1", target_port)) else {
                    return;
                };
                let mut client_read = client.try_clone().unwrap();
                let mut upstream_write = upstream.try_clone().unwrap();
                std::thread::spawn(move || {
                    let _ = std::io::copy(&mut client_read, &mut upstream_write);
                });
                let _ = std::io::copy(&mut upstream, &mut client);
            });
        }
    });

    (port, stop)
}

#[test]
fn orphan_with_intact_endpoint_is_reused() {
    let dir = fresh_dir("intact");
    let endpoint = orphan(&dir);

    let again = engine::resolve(&engine_exe(), &dir).expect("复用孤儿失败");
    assert_eq!(again.decision.as_str(), "reuse", "完好端点应当复用");
    assert!(
        !again.decision.owns_process(),
        "接管的引擎不是本次拉起的，退出时不该替用户关掉它"
    );
    assert_eq!(again.endpoint.port, endpoint.port, "必须连到同一个端口");

    cleanup(&dir, &endpoint);
}

#[test]
fn busy_engine_is_taken_over_instead_of_failing() {
    let dir = fresh_dir("busy");
    let real = orphan(&dir);

    // 端点指向代理：探测会超时，但引擎其实活着
    let (proxy_port, stop) = flaky_proxy(real.port);
    std::fs::write(
        engine::endpoint_path(&dir),
        format!(
            r#"{{"port":{},"token":"{}","pid":{}}}"#,
            proxy_port, real.token, real.pid.unwrap_or(0)
        ),
    )
    .unwrap();

    let resolved = engine::resolve(&engine_exe(), &dir);

    stop.store(true, Ordering::Relaxed);
    let result = match resolved {
        Ok(resolved) => {
            println!(
                "[引擎忙] decision={} 端口={}",
                resolved.decision.as_str(),
                resolved.endpoint.port
            );
            assert_eq!(
                resolved.decision.as_str(),
                "reuse",
                "探测失败但引擎其实活着：应当回头接管，而不是让用户起不来"
            );
            assert_eq!(resolved.endpoint.port, proxy_port, "应当连回端点里记的端口");
            true
        }
        Err(message) => {
            println!("[引擎忙] 失败：{}", message.lines().next().unwrap_or(""));
            false
        }
    };

    cleanup(&dir, &real);
    assert!(result, "引擎只是忙了一下，不该变成「打不开」");
}

#[test]
fn corrupted_endpoint_reports_actionable_error_and_keeps_address() {
    let dir = fresh_dir("corrupt");
    let real = orphan(&dir);

    // 端点被写坏（磁盘满、写到一半崩溃）。地址丢了 —— 这条没法自愈，
    // 引擎的端口和令牌只存在这一个文件里，没有第二个地方能问出来。
    let broken = r#"{"port":"#;
    std::fs::write(engine::endpoint_path(&dir), broken).unwrap();

    let message = match engine::resolve(&engine_exe(), &dir) {
        Ok(resolved) => {
            // 理论上也可能歪打正着复用上，那更好；但这里要断言「不会静默成功」
            panic!("端点损坏却返回了 {}，不该这样", resolved.decision.as_str())
        }
        Err(message) => message,
    };

    println!("[端点损坏] {}", message.lines().next().unwrap_or(""));

    assert!(
        message.contains("engine.exe"),
        "错误信息必须告诉用户去结束哪个进程，实际：{message}"
    );
    assert!(
        !message.contains("exit code"),
        "退出码对用户没有意义，实际：{message}"
    );

    // 端点文件必须被还原：留着坏内容至少说明「这里曾经有个引擎」，
    // 而空文件会让下一次启动以为「从没起过」，直接 Reuse 的机会也没了
    let after = std::fs::read(engine::endpoint_path(&dir)).unwrap_or_default();
    assert_eq!(
        String::from_utf8_lossy(&after),
        broken,
        "启动失败不该顺带把端点文件清空"
    );

    cleanup(&dir, &real);
}
