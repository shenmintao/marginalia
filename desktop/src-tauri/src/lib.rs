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

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use serde::Deserialize;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, RunEvent, WindowEvent,
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
fn resolve_bundled_python(app: &AppHandle) -> Option<(PathBuf, PathBuf)> {
    let resource_dir = app.path().resource_dir().ok()?;
    let backend_dir = resource_dir.join("backend");
    let manifest_path = backend_dir.join("runtime-manifest.json");
    let manifest_bytes = std::fs::read(&manifest_path)
        .map_err(|e| log::error!("missing runtime-manifest.json at {}: {}", manifest_path.display(), e))
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
}

impl BackendState {
    fn spawn(&self, app: &AppHandle) {
        if std::env::var("MARGINALIA_AUTOSTART_BACKEND")
            .map(|v| v == "0" || v.eq_ignore_ascii_case("false"))
            .unwrap_or(false)
        {
            log::info!("MARGINALIA_AUTOSTART_BACKEND=0, skipping backend spawn");
            return;
        }

        let home = marginalia_home();
        if let Err(e) = std::fs::create_dir_all(&home) {
            log::warn!("could not create MARGINALIA_HOME {}: {}", home.display(), e);
        }

        let mut cmd = if let Ok(cmd_str) = std::env::var("MARGINALIA_BACKEND_CMD") {
            let mut parts = cmd_str.split_whitespace();
            let Some(program) = parts.next() else {
                log::error!("MARGINALIA_BACKEND_CMD is empty");
                return;
            };
            let args: Vec<String> = parts.map(|s| s.to_string()).collect();
            log::info!("backend cmd from env: {}", cmd_str);
            let mut c = Command::new(program);
            c.args(&args);
            c
        } else {
            let Some((backend_dir, python)) = resolve_bundled_python(app) else {
                log::error!(
                    "no bundled backend found and MARGINALIA_BACKEND_CMD not set; \
                     the desktop build is missing its sidecar runtime"
                );
                return;
            };
            log::info!(
                "spawning bundled sidecar: {} -m marginalia (backend dir: {})",
                python.display(),
                backend_dir.display()
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

        match cmd
            .current_dir(&home)
            .env("MARGINALIA_HOME", &home)
            .env("PYTHONUNBUFFERED", "1")
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .spawn()
        {
            Ok(child) => {
                log::info!("spawned backend pid={} cwd={}", child.id(), home.display());
                *self.child.lock().unwrap() = Some(child);
            }
            Err(e) => {
                log::error!("failed to spawn backend: {}", e);
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
    }
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
        .manage(BackendState::default())
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
