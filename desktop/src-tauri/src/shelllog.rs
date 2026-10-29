//! 壳的日志。
//!
//! 存在的理由不是「方便调试」，而是**让端到端连通可以被判定**。
//! 「窗口打开了」不能证明前端拿到了端点并成功取到数据；日志里一行
//! `frontend_ready seq=N` 才能 —— 它由前端在首帧渲染完成后主动回报。
//!
//! 写失败静默：日志问题不该让应用崩掉，否则用户面对的是「打不开」而不是「没日志」。

use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub struct ShellLog {
    path: PathBuf,
}

impl ShellLog {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn write(&self, message: &str) {
        let line = format!("{} {}\n", unix_ms(), message);
        if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(&self.path) {
            let _ = file.write_all(line.as_bytes());
        }
    }
}

pub fn unix_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}
