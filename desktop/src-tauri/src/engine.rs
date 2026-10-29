//! 引擎发现与拉起。
//!
//! 三情形判断照抄 `desktop/launcher.py`，那套逻辑已有 10 项断言覆盖：
//!
//! 1. 没有 endpoint 文件            → 拉起（Spawn）
//! 2. 有文件且 `/health` 200        → 复用（Reuse），退出时**不关机**
//! 3. 有文件但 `/health` 不通       → 清残留再拉起（Respawn）
//!
//! 第三种是最容易写错的一种：只读文件不查健康，就会连到一个已经不存在的端口，
//! 表现为「应用打开了但永远转圈」。所以这里的顺序固定是「读文件 → 查健康 → 再决策」，
//! 不允许调换。
//!
//! HTTP 客户端手写，不引 reqwest：判断健康只需要「TCP 连得上 + 状态行是 200」。
//! 引擎侧的 HTTP 也是手写 Winsock，两侧保持同一量级。

use std::io::{BufRead, BufReader, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

/// 子进程不要弹控制台窗口。不加这个，双击壳会先闪一个黑框。
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub const ENDPOINT_NAME: &str = "engine.endpoint.json";
pub const SPAWN_TIMEOUT: Duration = Duration::from_secs(20);
const HEALTH_TIMEOUT: Duration = Duration::from_millis(1500);

/// 端点文件的内容。`pid` 缺失不算错误 —— 兼容旧格式，它只用于日志。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Endpoint {
    pub port: u16,
    pub token: String,
    pub pid: Option<u32>,
}

impl Endpoint {
    pub fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }
}

/// 决策结果。与 `Plan` 的区别：`Plan` 是「该怎么做」，`Decision` 是「做了什么」。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    Reuse,
    Spawn,
    Respawn,
}

impl Decision {
    /// 只有自己拉起的引擎才归我们管，退出时要优雅关掉。
    /// 复用的实例可能是用户自己开的，关它等于替用户做决定。
    pub fn owns_process(self) -> bool {
        matches!(self, Decision::Spawn | Decision::Respawn)
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Decision::Reuse => "reuse",
            Decision::Spawn => "spawn",
            Decision::Respawn => "respawn",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Plan {
    Reuse(Endpoint),
    Respawn(Endpoint),
    Spawn,
}

pub struct Resolved {
    pub endpoint: Endpoint,
    pub decision: Decision,
    pub child: Option<Child>,
}

/// 读端点文件。任何异常都返回 `None`。
///
/// 损坏的文件与不存在的文件在决策上等价（都要重新拉起），区分它们只会让调用方
/// 多写一个永远走不到的分支。真正需要区分的信息由日志承担。
///
/// 不要求整个文件是合法 UTF-8：端口、令牌、pid 全是 ASCII，就算某个字段的编码坏了，
/// 这里照样能拼出可用的端点。曾经用 `read_to_string` 硬要求 UTF-8，换来的是
/// 「引擎明明起来了、端点文件明明写出来了，壳却等 20 秒然后报超时」——
/// 旧版引擎把 ANSI 字节原样写进 JSON，中文路径下必然触发。
/// 编码正确性该由引擎负责，壳这边多一层容错是为了不让一个字段拖垮整个应用。
pub fn read_endpoint(data_dir: &Path) -> Option<Endpoint> {
    let bytes = std::fs::read(data_dir.join(ENDPOINT_NAME)).ok()?;
    let raw = String::from_utf8_lossy(&bytes);
    let value: serde_json::Value = serde_json::from_str(&raw).ok()?;
    let port = value.get("port")?.as_u64()?;
    let token = value.get("token")?.as_str()?;
    if port == 0 || port > u16::MAX as u64 || token.is_empty() {
        return None;
    }
    Some(Endpoint {
        port: port as u16,
        token: token.to_string(),
        pid: value.get("pid").and_then(|v| v.as_u64()).map(|v| v as u32),
    })
}

/// 纯决策。抽出来是为了能被单测覆盖 —— 真正的 `resolve` 要起进程、开端口，测不动。
pub fn plan(endpoint: Option<Endpoint>, healthy: bool) -> Plan {
    match endpoint {
        // 没有端点就没得复用，healthy 无意义
        None => Plan::Spawn,
        Some(endpoint) if healthy => Plan::Reuse(endpoint),
        // 有端点但连不上：上次异常退出留下的残留
        Some(endpoint) => Plan::Respawn(endpoint),
    }
}

/// 健康探测：只读状态行，不等响应体。
///
/// 依赖「服务端读完就关连接」会让超时判断变模糊 —— 引擎若将来改成 keep-alive，
/// 读完整响应就会一直等到超时，把健康的实例误判成不健康。
pub fn health_ok(endpoint: &Endpoint, timeout: Duration) -> bool {
    matches!(probe_status(endpoint, timeout), Some(200))
}

fn probe_status(endpoint: &Endpoint, timeout: Duration) -> Option<u16> {
    let addr = SocketAddr::from(([127, 0, 0, 1], endpoint.port));
    let mut stream = TcpStream::connect_timeout(&addr, timeout).ok()?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.set_write_timeout(Some(timeout)).ok()?;

    let request = format!(
        "GET /health HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n\
         Connection: close\r\n\r\n",
        endpoint.port, endpoint.token
    );
    stream.write_all(request.as_bytes()).ok()?;

    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    reader.read_line(&mut line).ok()?;
    parse_status(&line)
}

/// 从 `HTTP/1.1 200 OK` 里取状态码。
fn parse_status(line: &str) -> Option<u16> {
    let mut parts = line.split_whitespace();
    let proto = parts.next()?;
    if !proto.starts_with("HTTP/") {
        return None;
    }
    parts.next()?.parse().ok()
}

/// 拉起引擎并等到它真正能收请求。
///
/// 「发现文件写了」不等于「能收请求」—— 引擎先监听再写文件，但两者之间仍有窗口期，
/// 所以拿到端点后必须补一次健康检查，不能直接返回。
pub fn spawn(engine_exe: &Path, data_dir: &Path) -> Result<Resolved, String> {
    if !engine_exe.exists() {
        return Err(format!(
            "找不到引擎 {}（先跑 python toolchain/build.py engine）",
            engine_exe.display()
        ));
    }
    std::fs::create_dir_all(data_dir)
        .map_err(|e| format!("创建数据目录 {} 失败：{e}", data_dir.display()))?;

    // 残留文件会让下面的等待循环在第一次迭代就误判为「已就绪」
    let stale = data_dir.join(ENDPOINT_NAME);
    if stale.exists() {
        let _ = std::fs::remove_file(&stale);
    }

    let stderr_path = data_dir.join("engine.stderr.log");
    let stderr = std::fs::File::create(&stderr_path)
        .map_err(|e| format!("创建 {} 失败：{e}", stderr_path.display()))?;

    let mut command = Command::new(engine_exe);
    command
        .arg("--data-dir")
        .arg(data_dir)
        .arg("--http-port")
        .arg("0")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        // 写文件而不是管道：管道没人读会被写满，引擎在 stderr 上多输出几行就会卡住
        .stderr(Stdio::from(stderr));
    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);

    let mut child = command
        .spawn()
        .map_err(|e| format!("启动 {} 失败：{e}", engine_exe.display()))?;

    let deadline = Instant::now() + SPAWN_TIMEOUT;
    while Instant::now() < deadline {
        if let Ok(Some(status)) = child.try_wait() {
            let tail = read_tail(&stderr_path, 800);
            return Err(format!(
                "引擎提前退出（{status}）\n{tail}"
            ));
        }
        if let Some(endpoint) = read_endpoint(data_dir) {
            if health_ok(&endpoint, HEALTH_TIMEOUT) {
                return Ok(Resolved {
                    endpoint,
                    decision: Decision::Spawn,
                    child: Some(child),
                });
            }
        }
        std::thread::sleep(Duration::from_millis(50));
    }

    let _ = child.kill();
    let tail = read_tail(&stderr_path, 800);
    Err(format!(
        "等待引擎就绪超时（{}s）；{}\n{tail}",
        SPAWN_TIMEOUT.as_secs(),
        diagnose_endpoint(data_dir)
    ))
}

/// 超时时说明端点文件的真实状态。
///
/// 「超时」这一个词覆盖了三种完全不同的原因：文件没生成（引擎起不来）、
/// 文件生成了但解析不出（编码或字段问题）、解析出来了但连不上（端口没监听）。
/// 三种原因的排查方向完全不同，只报「超时」等于把定位工作推给下一个人。
fn diagnose_endpoint(data_dir: &Path) -> String {
    let path = data_dir.join(ENDPOINT_NAME);
    let bytes = match std::fs::read(&path) {
        Ok(bytes) => bytes,
        Err(_) => return "端点文件未生成（引擎可能启动即退出）".to_string(),
    };

    let mut note = format!("端点文件已生成 {} 字节", bytes.len());
    if std::str::from_utf8(&bytes).is_err() {
        note.push_str("，但不是合法 UTF-8（引擎版本过旧，把 ANSI 路径原样写进了 JSON）");
    }
    match read_endpoint(data_dir) {
        None => note.push_str("，解析不出 port/token"),
        Some(endpoint) => note.push_str(&format!(
            "，端点 port={} 可解析但健康检查未通过",
            endpoint.port
        )),
    }
    note
}

fn read_tail(path: &Path, max: usize) -> String {
    let text = std::fs::read_to_string(path).unwrap_or_default();
    let trimmed = text.trim_end();
    if trimmed.len() <= max {
        return trimmed.to_string();
    }
    // 保留尾部：错误信息一般在最后
    let start = trimmed.len() - max;
    let slice = &trimmed[start..];
    format!("…{}", slice.trim_start())
}

/// 完整解析流程：读端点 → 查健康 → 决策 → 必要时拉起。
pub fn resolve(engine_exe: &Path, data_dir: &Path) -> Result<Resolved, String> {
    let existing = read_endpoint(data_dir);
    let healthy = existing
        .as_ref()
        .map(|endpoint| health_ok(endpoint, HEALTH_TIMEOUT))
        .unwrap_or(false);
    let planned = plan(existing, healthy);

    match planned {
        Plan::Reuse(endpoint) => Ok(Resolved {
            endpoint,
            decision: Decision::Reuse,
            child: None,
        }),
        Plan::Respawn(_) => {
            let mut resolved = spawn(engine_exe, data_dir)?;
            resolved.decision = Decision::Respawn;
            Ok(resolved)
        }
        Plan::Spawn => spawn(engine_exe, data_dir),
    }
}

/// 优雅退出：让引擎自己落盘再退。
///
/// 不用 taskkill —— 引擎要靠优雅退出写 resume 数据，直接杀会丢任务列表。
pub fn shutdown(endpoint: &Endpoint) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], endpoint.port));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_secs(2)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let request = format!(
        "POST /shutdown HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n\
         Content-Length: 0\r\nConnection: close\r\n\r\n",
        endpoint.port, endpoint.token
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    if reader.read_line(&mut line).is_err() {
        return false;
    }
    matches!(parse_status(&line), Some(code) if code == 200)
}

/// 端点文件路径（给日志和测试用）。
pub fn endpoint_path(data_dir: &Path) -> PathBuf {
    data_dir.join(ENDPOINT_NAME)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;
    use std::net::TcpListener;

    fn endpoint() -> Endpoint {
        Endpoint {
            port: 12345,
            token: "abc".into(),
            pid: Some(7),
        }
    }

    /// 起一个只会说一句话的假服务器，返回端口。
    /// 真引擎也能测，但那需要 engine.exe 与真实进程，属于集成测试（tests/live_engine.rs）。
    fn fake_server(status: &'static str) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut socket, _)) = listener.accept() {
                let mut buf = [0u8; 512];
                let _ = socket.read(&mut buf);
                let response = format!("HTTP/1.1 {status}\r\nContent-Length: 2\r\n\r\nok");
                let _ = socket.write_all(response.as_bytes());
            }
        });
        port
    }

    // ---------- parse_status ----------

    #[test]
    fn parse_status_reads_code() {
        assert_eq!(parse_status("HTTP/1.1 200 OK\r\n"), Some(200));
        assert_eq!(parse_status("HTTP/1.1 401 Unauthorized\r\n"), Some(401));
    }

    #[test]
    fn parse_status_rejects_garbage() {
        assert_eq!(parse_status(""), None);
        assert_eq!(parse_status("500 Oops\r\n"), None);
        assert_eq!(parse_status("HTTP/1.1 abc OK\r\n"), None);
    }

    // ---------- read_endpoint ----------

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "btclient-test-{tag}-{}-{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn read_endpoint_ok() {
        let dir = temp_dir("ep-ok");
        std::fs::write(
            dir.join(ENDPOINT_NAME),
            r#"{"port":34567,"token":"tok","pid":99}"#,
        )
        .unwrap();
        let endpoint = read_endpoint(&dir).unwrap();
        assert_eq!(endpoint.port, 34567);
        assert_eq!(endpoint.token, "tok");
        assert_eq!(endpoint.pid, Some(99));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn read_endpoint_tolerates_missing_pid() {
        let dir = temp_dir("ep-nopid");
        std::fs::write(dir.join(ENDPOINT_NAME), r#"{"port":1,"token":"t"}"#).unwrap();
        assert_eq!(read_endpoint(&dir).unwrap().pid, None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn read_endpoint_rejects_broken_forms() {
        for (name, body) in [
            ("missing-file", None),
            ("bad-json", Some("{not json")),
            ("no-port", Some(r#"{"token":"t"}"#)),
            ("no-token", Some(r#"{"port":1000}"#)),
            ("empty-token", Some(r#"{"port":1000,"token":""}"#)),
            ("zero-port", Some(r#"{"port":0,"token":"t"}"#)),
            ("port-overflow", Some(r#"{"port":70000,"token":"t"}"#)),
        ] {
            let dir = temp_dir(name);
            if let Some(body) = body {
                std::fs::write(dir.join(ENDPOINT_NAME), body).unwrap();
            }
            assert!(
                read_endpoint(&dir).is_none(),
                "{name} 应当被判为不可用"
            );
            let _ = std::fs::remove_dir_all(&dir);
        }
    }

    // ---------- plan：三情形 ----------

    #[test]
    fn plan_without_endpoint_spawns() {
        assert_eq!(plan(None, false), Plan::Spawn);
        // 没有端点时 healthy 无意义，但也不能因此变成 Reuse
        assert_eq!(plan(None, true), Plan::Spawn);
    }

    #[test]
    fn plan_with_healthy_endpoint_reuses() {
        assert_eq!(plan(Some(endpoint()), true), Plan::Reuse(endpoint()));
    }

    #[test]
    fn plan_with_stale_endpoint_respawns() {
        assert_eq!(plan(Some(endpoint()), false), Plan::Respawn(endpoint()));
    }

    #[test]
    fn only_owned_decisions_shut_down_engine() {
        assert!(!Decision::Reuse.owns_process());
        assert!(Decision::Spawn.owns_process());
        assert!(Decision::Respawn.owns_process());
    }

    // ---------- health_ok ----------

    #[test]
    fn health_ok_true_on_200() {
        let port = fake_server("200 OK");
        let ep = Endpoint {
            port,
            token: "t".into(),
            pid: None,
        };
        assert!(health_ok(&ep, Duration::from_secs(2)));
    }

    #[test]
    fn health_ok_false_on_non_200() {
        let port = fake_server("401 Unauthorized");
        let ep = Endpoint {
            port,
            token: "wrong".into(),
            pid: None,
        };
        assert!(!health_ok(&ep, Duration::from_secs(2)));
    }

    #[test]
    fn health_ok_false_when_nothing_listens() {
        // 绑一个端口再立刻释放，得到一个几乎肯定没人监听的端口号
        let port = {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            listener.local_addr().unwrap().port()
        };
        let ep = Endpoint {
            port,
            token: "t".into(),
            pid: None,
        };
        assert!(!health_ok(&ep, Duration::from_millis(200)));
    }

    #[test]
    fn base_url_is_loopback() {
        assert_eq!(endpoint().base_url(), "http://127.0.0.1:12345");
    }
}
