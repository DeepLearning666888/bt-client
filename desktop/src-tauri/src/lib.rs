//! BT 客户端桌面壳。
//!
//! 职责只有三件，做完就交给前端：
//! 1. 保证只有一个壳在跑（命名 Mutex）
//! 2. 发现或拉起引擎（三情形判断，见 `engine`）
//! 3. 把端点注入窗口，并在退出时优雅关掉自己拉起的引擎
//!
//! 界面的事一律不碰 —— M2a 的前端已经能用，壳不该往里加逻辑。

pub mod engine;
pub mod shelllog;

use std::path::PathBuf;
use std::sync::Mutex;

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

use engine::{Decision, Endpoint};
use shelllog::ShellLog;

/// 应用数据目录。
///
/// 不用 `app.path().app_data_dir()`：单实例检查发生在应用构建之前，那时还没有
/// AppHandle。两处必须用同一个路径，否则日志会分裂成两个文件，排查时看到的是
/// 两份都不完整的记录。Windows 上 `%APPDATA%\<identifier>` 的规则是确定的。
///
/// `BTCLIENT_DATA_DIR` 可以覆盖。存在的理由不是「方便测试」——测试只是顺带受益：
/// 开发时想同时跑两个互不干扰的实例、或者验证「全新安装」的首次启动路径，
/// 都需要换一个干净的数据目录，而有测试价值则是因为它能隔离真实用户数据。
pub fn default_data_dir() -> PathBuf {
    if let Ok(override_dir) = std::env::var("BTCLIENT_DATA_DIR") {
        if !override_dir.trim().is_empty() {
            return PathBuf::from(override_dir);
        }
    }
    let base = std::env::var("APPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|_| std::env::temp_dir());
    base.join("com.btclient.desktop")
}

pub struct ShellState {
    pub log: ShellLog,
    pub endpoint: Endpoint,
    pub decision: Decision,
    /// 只有自己拉起的引擎才有句柄。留着是为了 shutdown 失败时兜底 kill。
    pub child: Mutex<Option<std::process::Child>>,
    /// 必须在整个进程生命周期内持有：提前 drop 等于释放单实例锁。
    pub _lock: single::InstanceLock,
}

/// 前端首帧渲染完成后回报一次。
///
/// 参数刻意只带 port 与 seq：port 能对上说明注入的端点是这一份，
/// seq 非空说明 `/state` 真的返回了数据。两个字段合起来才构成「端到端连通」。
#[tauri::command]
fn frontend_ready(state: tauri::State<'_, ShellState>, port: u16, seq: u64) -> Result<(), String> {
    state
        .log
        .write(&format!("frontend_ready port={port} seq={seq}"));
    Ok(())
}

/// 生成注入脚本。
///
/// 在页面脚本执行之前写入，所以前端读 `window.__ENGINE__` 时它一定存在。
/// token 用 JSON 序列化而不是手工拼引号：令牌里可能出现引号或反斜杠。
fn init_script(endpoint: &Endpoint) -> String {
    let token = serde_json::to_string(&endpoint.token).unwrap_or_else(|_| "\"\"".to_string());
    format!(
        "(function(){{window.__ENGINE__=Object.freeze({{baseUrl:'{}',token:{}}});}})();",
        endpoint.base_url(),
        token
    )
}

/// 定位 engine.exe。
///
/// 优先资源目录（打包形态），回退到仓库构建产物（开发形态）。
/// 两种形态都要能跑，否则「开发时好好的，装完就打不开」这种问题会留到发布才发现。
fn locate_engine(app: &tauri::AppHandle) -> PathBuf {
    if let Ok(resolved) = app
        .path()
        .resolve("engine.exe", tauri::path::BaseDirectory::Resource)
    {
        if resolved.exists() {
            return resolved;
        }
    }
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../engine/build/engine.exe")
}

fn setup(app: &mut tauri::App) -> Result<(), String> {
    let data_dir = default_data_dir();
    std::fs::create_dir_all(&data_dir)
        .map_err(|e| format!("创建数据目录 {} 失败：{e}", data_dir.display()))?;
    let log = ShellLog::new(data_dir.join("shell.log"));
    log.write("---- shell starting ----");
    log.write(&format!("data_dir={}", data_dir.display()));

    let lock = match single::acquire(&data_dir) {
        single::AcquireResult::Acquired(lock) => lock,
        single::AcquireResult::AlreadyRunning => {
            // 第二个实例什么都不做。绝不能走到 spawn —— 引擎自己有单实例锁，
            // 第二个会以退出码 2 死掉，用户看到的是「点了图标没反应」。
            log.write("another instance is running, exiting");
            std::process::exit(0);
        }
        single::AcquireResult::Failed(code) => {
            // 宁可多开，也不让用户点不开。记下来以便排查。
            log.write(&format!("single-instance mutex failed, code={code}"));
            single::InstanceLock::noop()
        }
    };

    let engine_dir = data_dir.join("engine");
    let engine_exe = locate_engine(app.handle());
    log.write(&format!("engine_exe={}", engine_exe.display()));

    let resolved = match engine::resolve(&engine_exe, &engine_dir) {
        Ok(resolved) => resolved,
        Err(message) => {
            log.write(&format!("engine resolve failed: {message}"));
            return Err(message);
        }
    };
    log.write(&format!(
        "engine decision={} port={} pid={:?}",
        resolved.decision.as_str(),
        resolved.endpoint.port,
        resolved.endpoint.pid
    ));

    let script = init_script(&resolved.endpoint);
    let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("BT 客户端")
        .inner_size(1100.0, 720.0)
        .min_inner_size(720.0, 480.0)
        // 注入必须在页面脚本之前，build 之后改不了
        .initialization_script(&script)
        .build()
        .map_err(|e| format!("创建窗口失败：{e}"))?;
    let _ = window.show();
    log.write("window shown");

    app.manage(ShellState {
        log,
        endpoint: resolved.endpoint,
        decision: resolved.decision,
        child: Mutex::new(resolved.child),
        _lock: lock,
    });
    Ok(())
}

pub fn run() {
    let app = tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![frontend_ready])
        .setup(|app| setup(app).map_err(|message| Box::<dyn std::error::Error>::from(message)))
        .build(tauri::generate_context!())
        .expect("构建 Tauri 应用失败");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(state) = handle.try_state::<ShellState>() {
                if state.decision.owns_process() {
                    let ok = engine::shutdown(&state.endpoint);
                    state.log.write(&format!("shutdown engine ok={ok}"));
                    if !ok {
                        // shutdown 没成功说明引擎已经不响应了，别留一个占端口的孤进程
                        if let Ok(mut guard) = state.child.lock() {
                            if let Some(child) = guard.as_mut() {
                                let _ = child.kill();
                                state.log.write("engine killed after failed shutdown");
                            }
                        }
                    }
                } else {
                    // 复用的实例不是我们拉起的，关它等于替用户做决定
                    state.log.write("engine reused, left running");
                }
            }
        }
    });
}

/// 命名 Mutex 单实例。
///
/// 不用 `tauri-plugin-single-instance`：它要额外依赖，而且默认行为是「激活已有窗口」，
/// 那需要跨进程 IPC。这里的需求更小 —— 只要保证第二个实例不 spawn 引擎就够了，
/// 用户再点一次图标时窗口已在前台，不会真的去点第二次。
mod single {
    use std::collections::hash_map::DefaultHasher;
    use std::ffi::OsStr;
    use std::hash::{Hash, Hasher};
    use std::os::windows::ffi::OsStrExt;
    use std::path::Path;
    use std::ptr;

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateMutexW(
            attributes: *const core::ffi::c_void,
            initial_owner: i32,
            name: *const u16,
        ) -> *mut core::ffi::c_void;
        fn GetLastError() -> u32;
        fn CloseHandle(handle: *mut core::ffi::c_void) -> i32;
    }

    const ERROR_ALREADY_EXISTS: u32 = 183;
    /// `Local\` 前缀 = 当前登录会话内唯一。用 `Global\` 会让同一台机器上的
    /// 另一个用户也开不了，那不是我们要的限制。
    const MUTEX_PREFIX: &str = "Local\\btclient-shell-";

    pub struct InstanceLock {
        handle: *mut core::ffi::c_void,
    }

    // 句柄只用于 CloseHandle，跨线程传递是安全的；Rust 无法自动推导裸指针的 Send/Sync。
    unsafe impl Send for InstanceLock {}
    unsafe impl Sync for InstanceLock {}

    impl InstanceLock {
        /// 拿不到锁时用的占位：不持有任何句柄，drop 时什么都不做。
        pub fn noop() -> Self {
            Self {
                handle: ptr::null_mut(),
            }
        }
    }

    impl Drop for InstanceLock {
        fn drop(&mut self) {
            if !self.handle.is_null() {
                unsafe {
                    CloseHandle(self.handle);
                }
            }
        }
    }

    pub enum AcquireResult {
        Acquired(InstanceLock),
        AlreadyRunning,
        Failed(u32),
    }

    /// 互斥体名字带数据目录的哈希。
    ///
    /// 为什么不固定成一个名字：两个用**不同数据目录**的壳本来就该能并存 ——
    /// 它们连的是不同的引擎、不同的端口，互不干扰。把它们判成「重复启动」
    /// 只会让「同时开两个互不打扰的实例」这种正常需求变得做不到。
    /// 同一个数据目录才是真正的冲突：会抢同一个 engine.endpoint.json。
    ///
    /// `DefaultHasher` 的种子是固定的（不像 `RandomState` 每次随机），
    /// 所以跨进程算出的值一致。
    fn instance_key(data_dir: &Path) -> String {
        let mut hasher = DefaultHasher::new();
        // 大小写不敏感：Windows 路径 `C:\X` 与 `c:\x` 是同一个目录
        data_dir.to_string_lossy().to_lowercase().hash(&mut hasher);
        format!("{:016x}", hasher.finish())
    }

    pub fn acquire(data_dir: &Path) -> AcquireResult {
        let name = format!("{MUTEX_PREFIX}{}", instance_key(data_dir));
        let wide: Vec<u16> = OsStr::new(&name)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        let handle = unsafe { CreateMutexW(ptr::null(), 0, wide.as_ptr()) };
        if handle.is_null() {
            return AcquireResult::Failed(unsafe { GetLastError() });
        }
        // CreateMutexW 在已存在时返回有效句柄并置 last-error 为 183，
        // 所以必须查 error 而不是查句柄是否为空
        let last_error = unsafe { GetLastError() };
        if last_error == ERROR_ALREADY_EXISTS {
            unsafe {
                CloseHandle(handle);
            }
            return AcquireResult::AlreadyRunning;
        }
        AcquireResult::Acquired(InstanceLock { handle })
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::path::PathBuf;

        #[test]
        fn same_dir_same_key() {
            let a = PathBuf::from(r"C:\Users\x\AppData\Roaming\com.btclient.desktop");
            let b = PathBuf::from(r"C:\Users\x\AppData\Roaming\com.btclient.desktop");
            assert_eq!(instance_key(&a), instance_key(&b));
        }

        #[test]
        fn case_insensitive() {
            assert_eq!(
                instance_key(Path::new(r"C:\Data\App")),
                instance_key(Path::new(r"c:\data\app"))
            );
        }

        #[test]
        fn different_dirs_differ() {
            assert_ne!(
                instance_key(Path::new(r"C:\one")),
                instance_key(Path::new(r"C:\two"))
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn init_script_embeds_endpoint() {
        let endpoint = Endpoint {
            port: 45678,
            token: "tok-123".into(),
            pid: None,
        };
        let script = init_script(&endpoint);
        assert!(script.contains("http://127.0.0.1:45678"));
        assert!(script.contains("\"tok-123\""));
        assert!(script.contains("window.__ENGINE__"));
    }

    #[test]
    fn init_script_escapes_quotes_in_token() {
        let endpoint = Endpoint {
            port: 1,
            token: "a\"b\\c".into(),
            pid: None,
        };
        let script = init_script(&endpoint);
        // JSON 序列化后引号与反斜杠都被转义，注入不会破坏 JS 语法
        assert!(script.contains(r#""a\"b\\c""#), "实际：{script}");
    }

    #[test]
    fn data_dir_uses_identifier() {
        // 与 tauri.conf.json 的 identifier 保持一致；改一处忘另一处会让日志跑到别的地方
        assert!(default_data_dir().ends_with("com.btclient.desktop"));
    }
}
