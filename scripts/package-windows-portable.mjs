#!/usr/bin/env node
// Pack a Windows portable .zip out of a Tauri release build.
//
// What goes into the zip:
//   1. The Tauri release exe + its sibling DLLs / resources tree at
//      desktop/src-tauri/target/release/ — same folder NSIS itself
//      installs into.
//   2. A README-portable.txt explaining: no autoupdate, WebView2 must
//      already be installed, MARGINALIA_HOME defaults to %USERPROFILE%
//      \Marginalia.
//
// The zip is then dropped next to the NSIS installer so the release
// workflow's bundle glob picks it up.

import { existsSync, mkdirSync, readFileSync, writeFileSync, statSync, readdirSync, cpSync, rmSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, '..');

const releaseDir = path.join(projectRoot, 'desktop', 'src-tauri', 'target', 'release');
const nsisDir = path.join(releaseDir, 'bundle', 'nsis');

const productName = 'Marginalia';

const readPackageVersion = () => {
  const cargoToml = readFileSync(
    path.join(projectRoot, 'desktop', 'src-tauri', 'Cargo.toml'),
    'utf8',
  );
  const m = /^version\s*=\s*"([^"]+)"/m.exec(cargoToml);
  if (!m) throw new Error('cannot read version from desktop/src-tauri/Cargo.toml');
  return m[1];
};

const README = `${productName} Windows portable package
=========================================

This is the portable build of ${productName}. Unzip anywhere and run
${productName}.exe. No installer, no admin privileges, no system Python.

First-launch notes:

  - Microsoft Edge WebView2 Runtime must already be installed on this
    machine. Most up-to-date Windows 11 / Windows 10 installs already
    have it. If the window stays blank, install it from
    https://developer.microsoft.com/microsoft-edge/webview2/
  - The first launch may show a SmartScreen "Windows protected your PC"
    dialog because the binary is unsigned. Click "More info" -> "Run
    anyway" once. Subsequent launches go straight through.
  - User data (db, library, .env) lives in %USERPROFILE%\\Marginalia by
    default. Set MARGINALIA_HOME to relocate.
  - Portable builds do not auto-update. To upgrade: download a newer
    zip and replace this folder.
`;

const collectFiles = (dir, base = dir) => {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...collectFiles(full, base));
    } else if (entry.isFile()) {
      out.push(path.relative(base, full));
    }
  }
  return out;
};

const main = () => {
  // Cargo names the binary after the package (`marginalia-tauri`); the
  // bundle pipeline renames it to `${productName}.exe` only inside the
  // MSI/NSIS installers. We rename it in the portable zip so users see
  // a friendly executable name.
  const cargoExeName = 'marginalia-tauri.exe';
  const friendlyExeName = `${productName}.exe`;
  const exePath = path.join(releaseDir, cargoExeName);
  if (!existsSync(exePath)) {
    throw new Error(`Tauri release binary not found at ${exePath}. Did 'tauri build' succeed?`);
  }
  const resourceDir = path.join(releaseDir, 'resources');
  if (!existsSync(resourceDir)) {
    throw new Error(`Bundled resource dir not found at ${resourceDir}.`);
  }

  const version = readPackageVersion();
  const arch = process.arch === 'arm64' ? 'arm64' : 'x86_64';
  const zipBase = `${productName}_${version}_windows_${arch}_portable`;
  const stagingDir = path.join(releaseDir, '..', 'portable-staging');
  const stagingApp = path.join(stagingDir, zipBase);

  // Reset staging dir. Use fs.rmSync + fs.cpSync below so this works on
  // a plain Windows shell (PowerShell, cmd) — not just CI runners that
  // happen to ship Git Bash with `rm` / `cp` on PATH.
  if (existsSync(stagingDir)) {
    rmSync(stagingDir, { recursive: true, force: true });
  }
  mkdirSync(stagingApp, { recursive: true });

  // 1. Copy the binary and any sibling DLLs at release/. We deliberately
  //    skip the heavy build artifacts (.pdb, deps/, build/, .rlib) and
  //    only mirror what the NSIS installer would put on disk.
  const cp = (src, dst) => {
    cpSync(src, dst, { recursive: true });
  };
  cp(exePath, path.join(stagingApp, friendlyExeName));

  // WebView2Loader.dll lives next to the exe on Tauri Windows builds.
  for (const sibling of readdirSync(releaseDir)) {
    if (/\.(dll)$/i.test(sibling)) {
      cp(path.join(releaseDir, sibling), path.join(stagingApp, sibling));
    }
  }

  // 2. Mirror the resources/ tree (icons + bundled python sidecar).
  cp(resourceDir, path.join(stagingApp, 'resources'));

  // 3. Drop the README.
  writeFileSync(path.join(stagingApp, 'README-portable.txt'), README, 'utf8');

  // 4. Zip into the NSIS bundle dir so the workflow glob picks it up.
  mkdirSync(nsisDir, { recursive: true });
  const zipPath = path.join(nsisDir, `${zipBase}.zip`);

  // Use PowerShell's Compress-Archive so we don't need any extra
  // dependency on the runner. The -Force flag overwrites stale zips
  // from previous runs of the same workflow.
  const psCmd =
    `Compress-Archive -Path '${stagingApp.replace(/\\/g, '/')}/*' ` +
    `-DestinationPath '${zipPath.replace(/\\/g, '/')}' -Force`;
  const r = spawnSync('powershell', ['-NoProfile', '-Command', psCmd], { stdio: 'inherit' });
  if (r.status !== 0) {
    throw new Error(`Compress-Archive failed (exit=${r.status})`);
  }

  const stat = statSync(zipPath);
  console.log(`[package-portable] wrote ${zipPath} (${(stat.size / 1024 / 1024).toFixed(1)} MB)`);
  const fileCount = collectFiles(stagingApp).length;
  console.log(`[package-portable] zip contains ${fileCount} files from ${stagingApp}`);
};

try {
  main();
} catch (error) {
  console.error(error?.stack || String(error));
  process.exit(1);
}
