//! AC-02：真引擎可被拉起、可复用、退出走 `/shutdown`、残留能重建。
//!
//! 这一组断言用真实的 `engine.exe`，不是假服务器。区别在于它会暴露
//! 「端点文件写了但还收不了请求」「shutdown 后进程没退」这类只有真进程才有的问题。
//!
//! 四步正好覆盖三情形：
//! 1. 空目录        → Spawn
//! 2. 引擎活着       → Reuse（且不持有 child）
//! 3. shutdown 后    → Respawn（端点文件残留但进程已死）
//! 4. 再 shutdown    → 清场

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use btclient_shell::engine::{self, Decision};

fn engine_exe() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../engine/build/engine.exe")
}

fn fresh_dir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "btclient-live-{tag}-{}-{}",
        std::process::id(),
        shelllog_ms()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn shelllog_ms() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis()
}

/// 等引擎进程真正退出。shutdown 是「请求已收到」，不等于「进程已结束」。
fn wait_dead(endpoint: &engine::Endpoint, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if !engine::health_ok(endpoint, Duration::from_millis(300)) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}

#[test]
fn live_engine_three_cases() {
    let exe = engine_exe();
    if !exe.exists() {
        panic!(
            "找不到 {}，先跑 python toolchain/build.py engine",
            exe.display()
        );
    }
    let data_dir = fresh_dir("three");

    // ---- 1. 空目录 → Spawn
    let first = engine::resolve(&exe, &data_dir).expect("首次拉起失败");
    assert_eq!(first.decision, Decision::Spawn, "空目录应当走 Spawn");
    assert!(first.child.is_some(), "自己拉起的必须持有 child 句柄");
    assert!(
        engine::health_ok(&first.endpoint, Duration::from_secs(2)),
        "拉起后必须立刻健康"
    );
    assert!(
        engine::endpoint_path(&data_dir).exists(),
        "端点文件必须落在数据目录里"
    );
    let first_endpoint = first.endpoint.clone();

    // ---- 2. 引擎活着 → Reuse
    let second = engine::resolve(&exe, &data_dir).expect("复用失败");
    assert_eq!(second.decision, Decision::Reuse, "健康端点应当复用");
    assert!(second.child.is_none(), "复用不该持有 child，否则退出会误关别人的引擎");
    assert_eq!(
        second.endpoint.port, first_endpoint.port,
        "复用必须连到同一个端口"
    );

    // ---- 3. shutdown：优雅退出，并且引擎自己清理端点文件
    assert!(
        engine::shutdown(&first_endpoint),
        "POST /shutdown 应当返回 200"
    );
    assert!(
        wait_dead(&first_endpoint, Duration::from_secs(10)),
        "shutdown 后引擎必须在 10s 内停止响应"
    );
    assert!(
        !engine::endpoint_path(&data_dir).exists(),
        "优雅退出时引擎会删掉端点文件 —— 所以 Respawn 只发生在异常退出之后"
    );

    // ---- 4. 模拟「上次异常退出」：端点文件还在，但进程已经死了
    // 直接写回旧端点比 kill 进程更可控：端口确实没人监听，文件内容也是真格式
    std::fs::write(
        engine::endpoint_path(&data_dir),
        format!(
            r#"{{"port":{},"token":"{}","pid":1}}"#,
            first_endpoint.port, first_endpoint.token
        ),
    )
    .unwrap();

    let third = engine::resolve(&exe, &data_dir).expect("残留端点后重建失败");
    assert_eq!(
        third.decision,
        Decision::Respawn,
        "端点残留但进程已死，应当走 Respawn"
    );
    assert!(third.child.is_some());
    assert!(engine::health_ok(&third.endpoint, Duration::from_secs(2)));

    // ---- 5. 清场
    assert!(engine::shutdown(&third.endpoint));
    let _ = wait_dead(&third.endpoint, Duration::from_secs(10));
    let _ = std::fs::remove_dir_all(&data_dir);
}

#[test]
fn spawn_reports_missing_engine() {
    let data_dir = fresh_dir("missing");
    // 不用 expect_err：它要求 Ok 侧实现 Debug，而 Resolved 里的 Child 没有
    let err = match engine::resolve(Path::new("Z:/definitely/not/here/engine.exe"), &data_dir) {
        Ok(_) => panic!("不存在的引擎必须返回错误，而不是静默超时"),
        Err(message) => message,
    };
    assert!(err.contains("找不到引擎"), "实际错误：{err}");
    let _ = std::fs::remove_dir_all(&data_dir);
}
