//! mh-relay：宿主按会话起的 MCP 入口（2026-11-26 试）。
//!
//! 原来每段会话起一个 Python MCP 进程（各 30M，带上读视图缓存到过 250M）；现在每段会话起这个几 MB 的程序，
//! 连到常驻服务 `python3 -m mh.synapse.pull.mcp daemon` 的 unix socket，开头报一行“我是谁”
//! （认宿主、会话、目录用的几个环境变量和 cwd、父进程号），之后只搬字节。
//! 常驻服务没起：exec `--` 后面给的命令（原来的 Python MCP），宿主照常可用；设了 MH_DB（测试用库的拷贝）也直接 exec。
//!
//! 用法：
//!   mh-relay -- <连不上时 exec 的命令…>               （MCP 流式转发）
//!   mh-relay --hook <action> -- <连不上时 exec 的命令…> （一次性钩子：inject / associate / codex-start / codex-associate）
use std::env;
use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::Command;
use std::thread;

const ENV_KEYS: [&str; 6] = [
    "MH_HOST",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
    "MH_HOST_PID",
    "MH_DEVICE",
    "MH_CLIENT_HOME",
];

fn socket_path() -> PathBuf {
    let home = env::var_os("MH_HOME").map(PathBuf::from).unwrap_or_else(|| {
        PathBuf::from(env::var_os("HOME").unwrap_or_default()).join(".local/share/memory-harness")
    });
    home.join("run/mcp.sock")
}

fn quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn hello(hook: Option<&str>) -> String {
    let envs: Vec<String> = ENV_KEYS
        .iter()
        .filter_map(|k| env::var(k).ok().filter(|v| !v.is_empty()).map(|v| format!("{}:{}", quote(k), quote(&v))))
        .collect();
    let cwd = env::current_dir().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default();
    let head = match hook {
        Some(action) => format!("\"mode\":\"hook\",\"action\":{},", quote(action)),
        None => String::new(),
    };
    format!(
        "{{{}\"env\":{{{}}},\"cwd\":{},\"ppid\":{}}}\n",
        head,
        envs.join(","),
        quote(&cwd),
        std::os::unix::process::parent_id()
    )
}

fn fallback(args: &[String]) -> ! {
    if args.is_empty() {
        eprintln!("mh-relay: 连不上 {}，也没有 `--` 后的命令可退", socket_path().display());
        std::process::exit(1);
    }
    let err = Command::new(&args[0]).args(&args[1..]).exec();
    eprintln!("mh-relay: exec {} 失败：{}", args[0], err);
    std::process::exit(1);
}

fn run_hook(mut sock: UnixStream, action: &str, rest: &[String]) -> ! {
    let mut input = Vec::new();
    let _ = io::stdin().lock().read_to_end(&mut input);
    if sock.write_all(hello(Some(action)).as_bytes()).is_err()
        || sock.write_all(&input).is_err()
        || sock.shutdown(std::net::Shutdown::Write).is_err()
    {
        fallback(rest);
    }
    let mut resp = Vec::new();
    if sock.read_to_end(&mut resp).is_err() || resp.is_empty() {
        fallback(rest);
    }
    let nl = match resp.iter().position(|&b| b == b'\n') {
        Some(i) => i,
        None => fallback(rest),
    };
    let header = String::from_utf8_lossy(&resp[..nl]);
    let mut parts = header.split_whitespace();
    let code: i32 = parts.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    let out_len: usize = parts.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    let body = &resp[nl + 1..];
    let split = out_len.min(body.len());
    let _ = io::stdout().lock().write_all(&body[..split]);
    let _ = io::stdout().lock().flush();
    if split < body.len() {
        let _ = io::stderr().lock().write_all(&body[split..]);
        let _ = io::stderr().lock().flush();
    }
    std::process::exit(code);
}

fn main() {
    let argv: Vec<String> = env::args().collect();
    let dash = argv.iter().position(|a| a == "--");
    let head = match dash {
        Some(i) => &argv[1..i],
        None => &argv[1..],
    };
    let rest: Vec<String> = match dash {
        Some(i) => argv[i + 1..].to_vec(),
        None => Vec::new(),
    };
    let hook = head
        .windows(2)
        .find(|w| w[0] == "--hook")
        .map(|w| w[1].as_str());
    // 指了别的库（测试用库的拷贝）：常驻服务开的是正式库，不转过去
    if env::var_os("MH_DB").is_some_and(|v| !v.is_empty()) {
        fallback(&rest);
    }
    let mut sock = match UnixStream::connect(socket_path()) {
        Ok(s) => s,
        Err(_) => fallback(&rest),
    };
    if let Some(action) = hook {
        run_hook(sock, action, &rest);
    }
    if sock.write_all(hello(None).as_bytes()).is_err() {
        fallback(&rest);
    }
    let mut up = sock.try_clone().expect("clone socket");
    let pump = thread::spawn(move || {
        let mut buf = [0u8; 8192];
        let mut stdin = io::stdin().lock();
        loop {
            match stdin.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    if up.write_all(&buf[..n]).is_err() {
                        break;
                    }
                }
            }
        }
        let _ = up.shutdown(std::net::Shutdown::Write);
    });
    let mut stdout = io::stdout().lock();
    let mut buf = [0u8; 8192];
    loop {
        match sock.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                if stdout.write_all(&buf[..n]).is_err() || stdout.flush().is_err() {
                    break;
                }
            }
        }
    }
    drop(pump);
}
