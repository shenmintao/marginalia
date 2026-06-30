//! Marginalia desktop shell.
//!
//! Wraps the React frontend in a Tauri window and:
//!   - Spawns the bundled Python sidecar (a python-build-standalone
//!     runtime carrying `marginalia` as an installed package) on launch.
//!     Tears it down on quit.
//!   - Hides the window to a system tray on close instead of exiting.
//!   - Tray menu: Show / Hide / Quit.
//!
//! Sidecar resolution order, per environment variable, then bundle:
//!   1. MARGINALIA_AUTOSTART_BACKEND=0 -> skip spawn entirely. Use this
//!      in dev when you're running `uvicorn marginalia.main:app` in
//!      another terminal yourself.
//!   2. MARGINALIA_BACKEND_CMD set -> split on whitespace; the first
//!      token is the binary, the rest are args. Honored verbatim, no
//!      bundle lookup. Useful for pointing a dev build at a checkout.
//!   3. Otherwise: read `<resource_dir>/backend/runtime-manifest.json`
//!      and run `<resource_dir>/backend/<manifest.python> -m marginalia`.
//!
//! Working directory is `MARGINALIA_HOME` (defaults to ~/Marginalia)
//! before spawn. pydantic-settings reads `.env` relative to CWD, so
//! that's also where the packaged app picks up `.env` — users get one
//! directory to manage (db + library + .env).

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

/// CREATE_NO_WINDOW from winbase.h. Set on Windows so spawning the
/// console-subsystem python.exe child from this windows-subsystem
/// parent doesn't make Windows allocate a fresh black console window
/// for the sidecar (the parent has none to inherit).
#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

use serde::Deserialize;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, RunEvent, State, WindowEvent,
};

fn home_dir() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_default()
}

fn marginalia_home() -> PathBuf {
    std::env::var_os("MARGINALIA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home_dir().join("Marginalia"))
}

#[derive(Debug, Deserialize)]
struct RuntimeManifest {
    /// Path to the python interpreter relative to the backend dir.
    python: String,
}

/// Locate the bundled Python interpreter under the resource dir.
///
/// The `bundle.resources` glob in `tauri.conf.json` is `resources/backend/**/*`
/// — Tauri preserves the leading `resources/` path segment when staging files
/// into the bundle, so at runtime the tree lives at
/// `<resource_dir>/resources/backend/`. The portable-zip layout matches this.
fn resolve_bundled_python(app: &AppHandle) -> Option<(PathBuf, PathBuf)> {
    let resource_dir = app.path().resource_dir().ok()?;
    let backend_dir = resource_dir.join("resources").join("backend");
    let manifest_path = backend_dir.join("runtime-manifest.json");
    let manifest_bytes = std::fs::read(&manifest_path)
        .map_err(|e| {
            log::error!(
                "missing runtime-manifest.json at {}: {}",
                manifest_path.display(),
                e
            )
        })
        .ok()?;
    let manifest: RuntimeManifest = serde_json::from_slice(&manifest_bytes)
        .map_err(|e| log::error!("invalid runtime-manifest.json: {}", e))
        .ok()?;
    let python = backend_dir.join(&manifest.python);
    if !python.is_file() {
        log::error!("manifest python not found at {}", python.display());
        return None;
    }
    Some((backend_dir, python))
}

#[derive(Default)]
struct BackendState {
    child: Mutex<Option<Child>>,
    port: Mutex<Option<u16>>,
    base_url: Mutex<Option<String>>,
}

/// Pick an ephemeral port the OS marks as currently free. There's a small
/// TOCTOU window between drop and the python sidecar's bind, but the OS is
/// unlikely to hand the same port out twice in that window.
fn pick_free_port() -> std::io::Result<u16> {
    let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
    let port = listener.local_addr()?.port();
    drop(listener);
    Ok(port)
}

#[derive(Debug, Deserialize)]
struct ServerDiscoveryState {
    base_url: String,
}

fn normalize_base_url(url: &str) -> String {
    url.trim().trim_end_matches('/').to_string()
}

fn configured_env_value(home: &Path, key: &str) -> Option<String> {
    if let Ok(value) = std::env::var(key) {
        let value = value.trim().to_string();
        if !value.is_empty() {
            return Some(value);
        }
    }

    let text = std::fs::read_to_string(home.join(".env")).ok()?;
    for raw_line in text.lines() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((name, raw_value)) = line.split_once('=') else {
            continue;
        };
        if !name.trim().eq_ignore_ascii_case(key) {
            continue;
        }
        let mut value = raw_value.trim().to_string();
        if value.len() >= 2 {
            let bytes = value.as_bytes();
            let quoted = (bytes[0] == b'"' && bytes[value.len() - 1] == b'"')
                || (bytes[0] == b'\'' && bytes[value.len() - 1] == b'\'');
            if quoted {
                value = value[1..value.len() - 1].to_string();
            } else if let Some(comment_at) = value.find(" #") {
                value.truncate(comment_at);
                value = value.trim_end().to_string();
            }
        }
        if !value.is_empty() {
            return Some(value);
        }
    }
    None
}

fn configured_api_host(home: &Path) -> String {
    configured_env_value(home, "MARGINALIA_API_HOST").unwrap_or_else(|| "127.0.0.1".to_string())
}

fn configured_api_port(home: &Path) -> Option<u16> {
    configured_env_value(home, "MARGINALIA_API_PORT").and_then(|value| match value.parse::<u16>() {
        Ok(port) => Some(port),
        Err(e) => {
            log::warn!("ignoring invalid MARGINALIA_API_PORT={}: {}", value, e);
            None
        }
    })
}

fn client_base_url(host: &str, port: u16) -> String {
    let mut client_host = host.trim();
    if client_host.is_empty() || client_host == "0.0.0.0" || client_host == "::" {
        client_host = "127.0.0.1";
    }
    if client_host.contains(':') && !client_host.starts_with('[') {
        format!("http://[{}]:{}", client_host, port)
    } else {
        format!("http://{}:{}", client_host, port)
    }
}

fn local_port_from_base_url(url: &str) -> Option<u16> {
    let rest = url
        .strip_prefix("http://127.0.0.1:")
        .or_else(|| url.strip_prefix("http://localhost:"))?;
    let port = rest.split('/').next().unwrap_or(rest);
    port.parse::<u16>().ok()
}

fn local_backend_health_ok(base_url: &str, port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(250)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(500)));
    let host = if base_url.starts_with("http://localhost:") {
        "localhost"
    } else {
        "127.0.0.1"
    };
    let request = format!(
        "GET /health HTTP/1.1\r\nHost: {}:{}\r\nConnection: close\r\n\r\n",
        host, port
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = [0_u8; 256];
    let Ok(n) = stream.read(&mut response) else {
        return false;
    };
    let head = String::from_utf8_lossy(&response[..n]);
    head.starts_with("HTTP/1.1 200") || head.starts_with("HTTP/1.0 200")
}

fn discover_backend(home: &Path) -> Option<String> {
    let state_path = home.join("runtime").join("server.json");
    let bytes = std::fs::read(&state_path).ok()?;
    let state: ServerDiscoveryState = match serde_json::from_slice(&bytes) {
        Ok(s) => s,
        Err(e) => {
            append_launcher_log(
                home,
                "warn",
                &format!(
                    "ignoring invalid backend discovery file {}: {}",
                    state_path.display(),
                    e
                ),
            );
            return None;
        }
    };
    let base_url = normalize_base_url(&state.base_url);
    let port = local_port_from_base_url(&base_url)?;
    if local_backend_health_ok(&base_url, port) {
        Some(base_url)
    } else {
        append_launcher_log(
            home,
            "warn",
            &format!(
                "ignoring stale backend discovery file {}; /health did not pass for {}",
                state_path.display(),
                base_url
            ),
        );
        None
    }
}

#[tauri::command]
fn backend_port(state: State<'_, BackendState>) -> Option<u16> {
    *state.port.lock().unwrap()
}

#[tauri::command]
fn backend_base_url(state: State<'_, BackendState>) -> Option<String> {
    state.base_url.lock().unwrap().clone()
}

#[tauri::command]
fn logs_dir() -> String {
    marginalia_home().join("logs").display().to_string()
}

#[tauri::command]
fn append_frontend_log(level: String, message: String) -> Result<(), String> {
    append_named_log(&marginalia_home(), "frontend.log", &level, &message)
        .map_err(|e| format!("could not write frontend log: {}", e))
}

impl BackendState {
    fn spawn(&self, app: &AppHandle) {
        let home = marginalia_home();
        if let Err(e) = std::fs::create_dir_all(&home) {
            log::warn!("could not create MARGINALIA_HOME {}: {}", home.display(), e);
        }
        append_launcher_log(
            &home,
            "info",
            &format!("desktop launch using MARGINALIA_HOME={}", home.display()),
        );
        // First-launch: drop a starter .env so users have somewhere to put
        // their LLM key. validate_llm_config still flags an empty key, but
        // the desktop launch path soft-fails (MARGINALIA_DESKTOP=1) so the
        // server still comes up and Settings → LLM Profile becomes reachable.
        ensure_starter_env(&home);

        if let Ok(server) = std::env::var("MARGINALIA_SERVER") {
            let server = normalize_base_url(&server);
            if !server.is_empty() {
                log::info!("using backend from MARGINALIA_SERVER: {}", server);
                append_launcher_log(
                    &home,
                    "info",
                    &format!("using backend from MARGINALIA_SERVER: {}", server),
                );
                *self.base_url.lock().unwrap() = Some(server);
                return;
            }
        }

        if let Some(discovered) = discover_backend(&home) {
            log::info!("using discovered backend: {}", discovered);
            append_launcher_log(
                &home,
                "info",
                &format!(
                    "using discovered backend from runtime/server.json: {}",
                    discovered
                ),
            );
            *self.base_url.lock().unwrap() = Some(discovered);
            return;
        }

        if std::env::var("MARGINALIA_AUTOSTART_BACKEND")
            .map(|v| v == "0" || v.eq_ignore_ascii_case("false"))
            .unwrap_or(false)
        {
            log::info!("MARGINALIA_AUTOSTART_BACKEND=0, skipping backend spawn");
            append_launcher_log(
                &home,
                "info",
                "MARGINALIA_AUTOSTART_BACKEND=0, skipping backend spawn",
            );
            return;
        }

        let host = configured_api_host(&home);
        let port = match configured_api_port(&home) {
            Some(p) => p,
            None => match pick_free_port() {
                Ok(p) => p,
                Err(e) => {
                    log::error!("failed to allocate ephemeral backend port: {}", e);
                    append_launcher_log(
                        &home,
                        "error",
                        &format!("failed to allocate ephemeral backend port: {}", e),
                    );
                    return;
                }
            },
        };
        let base_url = client_base_url(&host, port);
        *self.port.lock().unwrap() = Some(port);
        *self.base_url.lock().unwrap() = Some(base_url.clone());
        log::info!("backend url = {}", base_url);
        append_launcher_log(&home, "info", &format!("backend url = {}", base_url));

        let mut cmd = if let Ok(cmd_str) = std::env::var("MARGINALIA_BACKEND_CMD") {
            let mut parts = cmd_str.split_whitespace();
            let Some(program) = parts.next() else {
                log::error!("MARGINALIA_BACKEND_CMD is empty");
                append_launcher_log(&home, "error", "MARGINALIA_BACKEND_CMD is empty");
                return;
            };
            let args: Vec<String> = parts.map(|s| s.to_string()).collect();
            log::info!("backend cmd from env: {}", cmd_str);
            append_launcher_log(
                &home,
                "info",
                &format!("backend command from MARGINALIA_BACKEND_CMD: {}", cmd_str),
            );
            let mut c = Command::new(program);
            c.args(&args);
            c
        } else {
            let Some((backend_dir, python)) = resolve_bundled_python(app) else {
                log::error!(
                    "no bundled backend found and MARGINALIA_BACKEND_CMD not set; \
                     the desktop build is missing its sidecar runtime"
                );
                append_launcher_log(
                    &home,
                    "error",
                    "no bundled backend found and MARGINALIA_BACKEND_CMD not set",
                );
                return;
            };
            log::info!(
                "spawning bundled sidecar: {} -m marginalia (backend dir: {})",
                python.display(),
                backend_dir.display()
            );
            append_launcher_log(
                &home,
                "info",
                &format!(
                    "spawning bundled sidecar: {} -m marginalia (backend dir: {})",
                    python.display(),
                    backend_dir.display()
                ),
            );
            let mut c = Command::new(&python);
            c.arg("-m").arg("marginalia");
            // Help the interpreter find its own stdlib regardless of CWD,
            // and make sure the rest of the runtime tree (site-packages)
            // resolves cleanly when the user double-clicks the bundle.
            if let Some(home_dir) = python_home_for(&python) {
                c.env("PYTHONHOME", home_dir);
            }
            c
        };

        // Redirect sidecar stdout/stderr to a log file under MARGINALIA_HOME
        // so the user (and we) can read what happened on a crash. Inheriting
        // from the parent doesn't help here — the windows-subsystem parent
        // has no console to inherit from on a packaged build.
        let (stdout_target, stderr_target) = open_backend_log_streams(&home);

        cmd.current_dir(&home)
            .env("MARGINALIA_HOME", &home)
            .env("MARGINALIA_API_HOST", &host)
            .env("MARGINALIA_API_PORT", port.to_string())
            .env("MARGINALIA_HTTP_SERVER", "1")
            .env("MARGINALIA_DESKTOP", "1")
            .env("PYTHONUNBUFFERED", "1")
            .stdout(stdout_target)
            .stderr(stderr_target);

        // Suppress the auto-allocated console window for the python.exe child
        // on Windows. Without this flag the parent's windows-subsystem flag
        // doesn't propagate, so the OS gives the console-subsystem child its
        // own black window in the foreground.
        #[cfg(target_os = "windows")]
        cmd.creation_flags(CREATE_NO_WINDOW);

        match cmd.spawn() {
            Ok(child) => {
                log::info!("spawned backend pid={} cwd={}", child.id(), home.display());
                append_launcher_log(
                    &home,
                    "info",
                    &format!("spawned backend pid={} cwd={}", child.id(), home.display()),
                );
                *self.child.lock().unwrap() = Some(child);
            }
            Err(e) => {
                log::error!("failed to spawn backend: {}", e);
                append_launcher_log(&home, "error", &format!("failed to spawn backend: {}", e));
            }
        }
    }

    fn kill(&self) {
        if let Some(mut child) = self.child.lock().unwrap().take() {
            let pid = child.id();
            match child.kill() {
                Ok(_) => log::info!("killed backend pid={}", pid),
                Err(e) => log::warn!("backend pid={} kill failed: {}", pid, e),
            }
            let _ = child.wait();
        }
        *self.port.lock().unwrap() = None;
        *self.base_url.lock().unwrap() = None;
    }
}

/// Drop a starter `.env` into MARGINALIA_HOME on first launch so users
/// have somewhere obvious to paste their LLM key. We never overwrite an
/// existing file. If the write fails (read-only home, perms, etc.) we
/// just log — the server still comes up under MARGINALIA_DESKTOP=1.
fn ensure_starter_env(home: &Path) {
    let env_path = home.join(".env");
    if env_path.exists() {
        return;
    }
    let template = "\
# Marginalia configuration. Reload the desktop app after editing.
#
# Pick a provider for the chat / reflect / ingest profiles. The
# Settings page in the app writes these same fields — editing here
# or there is equivalent.
#
# OpenAI:
#   LLM_DEFAULT_PROVIDER=openai
#   LLM_DEFAULT_MODEL=gpt-4o-mini
#   LLM_DEFAULT_API_KEY=sk-...
#
# OpenAI-compatible (DeepSeek / Together / Groq / vllm / ollama):
#   LLM_DEFAULT_PROVIDER=openai-compatible
#   LLM_DEFAULT_BASE_URL=https://api.deepseek.com/v1
#   LLM_DEFAULT_MODEL=deepseek-chat
#   LLM_DEFAULT_API_KEY=sk-...
#
# Anthropic:
#   LLM_DEFAULT_PROVIDER=anthropic
#   LLM_DEFAULT_MODEL=claude-sonnet-4-5
#   LLM_DEFAULT_API_KEY=sk-ant-...

LLM_DEFAULT_PROVIDER=openai
LLM_DEFAULT_MODEL=gpt-4o-mini
LLM_DEFAULT_API_KEY=

# Backend bind settings. Change the port if another local service uses 8000.
MARGINALIA_API_HOST=127.0.0.1
MARGINALIA_API_PORT=8000
";
    match std::fs::write(&env_path, template) {
        Ok(_) => {
            log::info!("wrote starter .env at {}", env_path.display());
            append_launcher_log(
                home,
                "info",
                &format!("wrote starter .env at {}", env_path.display()),
            );
        }
        Err(e) => {
            log::warn!(
                "could not write starter .env at {}: {}",
                env_path.display(),
                e
            );
            append_launcher_log(
                home,
                "warn",
                &format!(
                    "could not write starter .env at {}: {}",
                    env_path.display(),
                    e
                ),
            );
        }
    }
}

fn append_launcher_log(home: &Path, level: &str, message: &str) {
    if let Err(e) = append_named_log(home, "launcher.log", level, message) {
        log::warn!("could not append launcher log: {}", e);
    }
}

fn append_named_log(home: &Path, name: &str, level: &str, message: &str) -> std::io::Result<()> {
    let logs = home.join("logs");
    std::fs::create_dir_all(&logs)?;
    let path = logs.join(name);
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    let level = sanitize_log_text(level, 16);
    let message = sanitize_log_text(message, 8_000);
    writeln!(file, "{} [{}] {}", log_timestamp(), level, message)
}

fn log_timestamp() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_else(|_| Duration::from_secs(0));
    format!("{}.{:03}Z", now.as_secs(), now.subsec_millis())
}

fn sanitize_log_text(value: &str, max_chars: usize) -> String {
    let mut out = String::new();
    let mut count = 0;
    for ch in value.chars() {
        if count >= max_chars {
            out.push_str("...");
            break;
        }
        match ch {
            '\r' => out.push_str("\\r"),
            '\n' => out.push_str("\\n"),
            '\t' => out.push('\t'),
            c if c.is_control() => out.push('?'),
            c => out.push(c),
        }
        count += 1;
    }
    out
}

/// Open append-mode handles for the sidecar's stdout / stderr so its
/// output survives across launches. Returns piped Stdio's that the
/// child can take over. Falls back to `Stdio::null()` if the log file
/// can't be opened — losing diagnostics is preferable to crashing the
/// app at startup over a logging permission error.
fn open_backend_log_streams(home: &Path) -> (Stdio, Stdio) {
    let logs = home.join("logs");
    if let Err(e) = std::fs::create_dir_all(&logs) {
        log::warn!("could not create logs dir {}: {}", logs.display(), e);
        append_launcher_log(
            home,
            "warn",
            &format!("could not create logs dir {}: {}", logs.display(), e),
        );
        return (Stdio::null(), Stdio::null());
    }
    let log_path = logs.join("backend.log");
    let file = match std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
    {
        Ok(f) => f,
        Err(e) => {
            log::warn!("could not open {}: {}", log_path.display(), e);
            append_launcher_log(
                home,
                "warn",
                &format!("could not open {}: {}", log_path.display(), e),
            );
            return (Stdio::null(), Stdio::null());
        }
    };
    let dup = match file.try_clone() {
        Ok(f) => f,
        Err(e) => {
            log::warn!("could not duplicate {} handle: {}", log_path.display(), e);
            append_launcher_log(
                home,
                "warn",
                &format!("could not duplicate {} handle: {}", log_path.display(), e),
            );
            return (Stdio::null(), Stdio::null());
        }
    };
    (Stdio::from(file), Stdio::from(dup))
}

/// PYTHONHOME for a python-build-standalone layout: on Windows the
/// interpreter sits at `<root>/python.exe`, on POSIX at `<root>/bin/python3`.
fn python_home_for(python: &Path) -> Option<PathBuf> {
    let parent = python.parent()?;
    if cfg!(target_os = "windows") {
        Some(parent.to_path_buf())
    } else {
        // bin/python3 -> root is parent.parent
        parent.parent().map(|p| p.to_path_buf())
    }
}

fn show_main_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

fn hide_main_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.hide();
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let show_i = MenuItem::with_id(app, "show", "Show Marginalia", true, None::<&str>)?;
    let hide_i = MenuItem::with_id(app, "hide", "Hide window", true, None::<&str>)?;
    let quit_i = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_i, &hide_i, &quit_i])?;

    let _tray = TrayIconBuilder::with_id("main-tray")
        .tooltip("Marginalia")
        .icon(app.default_window_icon().cloned().unwrap())
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main_window(app),
            "hide" => hide_main_window(app),
            "quit" => {
                if let Some(state) = app.try_state::<BackendState>() {
                    state.kill();
                }
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                let app = tray.app_handle();
                if let Some(w) = app.get_webview_window("main") {
                    if w.is_visible().unwrap_or(false) {
                        let _ = w.hide();
                    } else {
                        show_main_window(app);
                    }
                }
            }
        })
        .build(app)?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // Singleton: a second `Marginalia.exe` double-click hands its
        // argv to this callback and exits. Without it each launch spins
        // up a fresh Rust process, fresh webview, and fresh Python
        // sidecar on a different ephemeral port — multiple task runners
        // race over the same SQLite file. Hide-on-close (below) makes
        // this especially easy to trigger: the user "closes" the window
        // (just hidden), double-clicks the exe again to come back, and
        // gets a duplicate stack instead of the existing one.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            show_main_window(app);
        }))
        .manage(BackendState::default())
        .invoke_handler(tauri::generate_handler![
            backend_port,
            backend_base_url,
            logs_dir,
            append_frontend_log
        ])
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            build_tray(app.handle())?;
            let handle = app.handle().clone();
            app.state::<BackendState>().spawn(&handle);
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let RunEvent::ExitRequested { .. } = event {
                if let Some(state) = app.try_state::<BackendState>() {
                    state.kill();
                }
            }
        });
}
