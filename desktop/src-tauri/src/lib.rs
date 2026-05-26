//! Marginalia desktop shell.
//!
//! Wraps the React frontend in a Tauri window and:
//!   - Spawns the FastAPI backend on launch (if `MARGINALIA_AUTOSTART_BACKEND`
//!     is unset or "1") and tears it down on quit.
//!   - Hides the window to a system tray on close instead of exiting.
//!   - Tray menu: Show / Hide / Quit.
//!
//! The backend command defaults to `python -m marginalia.main`. Override
//! via `MARGINALIA_BACKEND_CMD` (the value is split on whitespace, first
//! token is the binary, rest are args). Set `MARGINALIA_AUTOSTART_BACKEND=0`
//! to skip the spawn entirely — useful when you're already running the
//! backend in a separate terminal during development.
//!
//! The backend's working directory is set to `MARGINALIA_HOME` (defaults
//! to `~/Marginalia`) before spawn. pydantic-settings resolves `.env`
//! relative to CWD, so this is where the packaged app reads `.env` from
//! — users get one directory to manage (db + library + .env all live
//! under it). In dev, run the backend yourself in a separate terminal
//! (`MARGINALIA_AUTOSTART_BACKEND=0`) so it picks up the project-root
//! `.env` instead.

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

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

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, RunEvent, WindowEvent,
};

#[derive(Default)]
struct BackendState {
    child: Mutex<Option<Child>>,
}

impl BackendState {
    fn spawn(&self) {
        if std::env::var("MARGINALIA_AUTOSTART_BACKEND")
            .map(|v| v == "0" || v.eq_ignore_ascii_case("false"))
            .unwrap_or(false)
        {
            log::info!("MARGINALIA_AUTOSTART_BACKEND=0, skipping backend spawn");
            return;
        }
        let cmd_str = std::env::var("MARGINALIA_BACKEND_CMD")
            .unwrap_or_else(|_| "python -m marginalia.main".to_string());
        let mut parts = cmd_str.split_whitespace();
        let Some(program) = parts.next() else {
            log::error!("MARGINALIA_BACKEND_CMD is empty");
            return;
        };
        let args: Vec<&str> = parts.collect();
        let home = marginalia_home();
        if let Err(e) = std::fs::create_dir_all(&home) {
            log::warn!("could not create MARGINALIA_HOME {}: {}", home.display(), e);
        }
        match Command::new(program)
            .args(&args)
            .current_dir(&home)
            .env("MARGINALIA_HOME", &home)
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .spawn()
        {
            Ok(child) => {
                log::info!(
                    "spawned backend pid={} cwd={}: {}",
                    child.id(),
                    home.display(),
                    cmd_str
                );
                *self.child.lock().unwrap() = Some(child);
            }
            Err(e) => {
                log::error!("failed to spawn backend ({}): {}", cmd_str, e);
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
            // Single left-click on the tray icon toggles the window —
            // the standard Windows/macOS expectation for tray-resident apps.
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
            app.state::<BackendState>().spawn();
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
