// Build-time helper: prepare a self-contained Python runtime + the
// installed marginalia package under `desktop/src-tauri/resources/backend/`.
// Tauri's bundle.resources picks this directory up and copies it into the
// final installer / .app / AppImage. At runtime, src-tauri/src/lib.rs
// resolves the resource dir and spawns `<dir>/python(.exe) -m marginalia`.
//
// Inspired by AstrBotDevs/AstrBot-desktop's prepare-resources pipeline
// (AGPL-3.0). See scripts/UPSTREAM.md for the full attribution. We dropped
// the AstrBot mode dispatcher / dual-repo source fetch / IPC bridge checks
// because Marginalia is single-repo and reuses Tauri's standard
// beforeBuildCommand for the frontend; the only thing left worth keeping
// is "fetch a known-good standalone CPython, install our package into it,
// drop a manifest, copy to bundle.resources".

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { cp, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, '..');

const PBS_RELEASE = process.env.MARGINALIA_PBS_RELEASE || '20260211';
const PBS_VERSION = process.env.MARGINALIA_PBS_VERSION || '3.12.12';

const PLATFORM_MAP = { linux: 'linux', darwin: 'mac', win32: 'windows' };
const TARGET_MAP = {
  'linux/x64': 'x86_64-unknown-linux-gnu',
  'linux/arm64': 'aarch64-unknown-linux-gnu',
  'mac/x64': 'x86_64-apple-darwin',
  'mac/arm64': 'aarch64-apple-darwin',
  'windows/x64': 'x86_64-pc-windows-msvc',
  'windows/arm64': 'aarch64-pc-windows-msvc',
};

const resolvePbsTarget = () => {
  const platform = PLATFORM_MAP[process.platform];
  const arch = process.arch === 'x64' ? 'x64' : process.arch === 'arm64' ? 'arm64' : process.arch;
  const key = `${platform}/${arch}`;
  const target = TARGET_MAP[key];
  if (!target) {
    throw new Error(`Unsupported platform/arch for python-build-standalone: ${process.platform}/${process.arch}`);
  }
  return target;
};

const runChecked = (cmd, args, cwd, env = {}) => {
  const result = spawnSync(cmd, args, {
    cwd,
    stdio: 'inherit',
    env: { ...process.env, ...env },
    shell: process.platform === 'win32' && (cmd === 'pnpm' || cmd === 'npm'),
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`Command failed (exit=${result.status}): ${cmd} ${args.join(' ')}`);
  }
};

const resolveRuntimePython = (root) => {
  const candidates = process.platform === 'win32'
    ? [path.join(root, 'python.exe')]
    : [path.join(root, 'bin', 'python3'), path.join(root, 'bin', 'python')];
  for (const c of candidates) if (existsSync(c)) return c;
  throw new Error(`Cannot find python executable under ${root}`);
};

const ensureCpythonRuntime = () => {
  const target = resolvePbsTarget();
  const runtimeBase = path.join(projectRoot, 'runtime', `${target}-${PBS_VERSION}`);
  const runtimeRoot = path.join(runtimeBase, 'marginalia-cpython-runtime');

  if (existsSync(runtimeRoot)) {
    console.log(`[prepare-backend] CPython runtime cached at ${runtimeRoot}`);
    return runtimeRoot;
  }

  mkdirSync(runtimeBase, { recursive: true });
  const resolverScript = path.join(projectRoot, 'scripts', 'cpython', 'resolve_packaged_cpython_runtime.py');
  const pythonCandidates = process.platform === 'win32' ? ['python', 'py'] : ['python3', 'python'];

  for (const cmd of pythonCandidates) {
    const args = cmd === 'py' ? ['-3', resolverScript] : [resolverScript];
    const result = spawnSync(cmd, args, {
      cwd: projectRoot,
      stdio: 'inherit',
      env: {
        ...process.env,
        RUNNER_TEMP_DIR: runtimeBase,
        PYTHON_BUILD_STANDALONE_RELEASE: PBS_RELEASE,
        PYTHON_BUILD_STANDALONE_VERSION: PBS_VERSION,
        PYTHON_BUILD_STANDALONE_TARGET: target,
      },
    });
    if (result.error?.code === 'ENOENT') continue;
    if (result.status !== 0) throw new Error(`resolve_packaged_cpython_runtime.py failed via ${cmd}.`);
    return runtimeRoot;
  }
  throw new Error('Cannot find a Python interpreter to bootstrap the standalone runtime.');
};

const installMarginaliaInto = (runtimeRoot) => {
  const py = resolveRuntimePython(runtimeRoot);
  console.log(`[prepare-backend] Installing marginalia into ${runtimeRoot} via ${py}`);
  runChecked(py, ['-m', 'pip', 'install', '--upgrade', 'pip'], projectRoot);
  runChecked(py, ['-m', 'pip', 'install', '--no-warn-script-location', '.'], projectRoot);
};

const copyRuntimeToBundleResources = async (runtimeRoot) => {
  const target = path.join(projectRoot, 'desktop', 'src-tauri', 'resources', 'backend');
  console.log(`[prepare-backend] Syncing ${runtimeRoot} -> ${target}`);
  await rm(target, { recursive: true, force: true });
  await cp(runtimeRoot, target, { recursive: true });

  const py = resolveRuntimePython(target);
  const relPython = path.relative(target, py).split(path.sep).join('/');
  const manifestPath = path.join(target, 'runtime-manifest.json');
  const manifest = {
    python: relPython,
    pbs_release: PBS_RELEASE,
    pbs_version: PBS_VERSION,
    target: resolvePbsTarget(),
    package: readPackageVersion(),
  };
  writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + '\n', 'utf8');
  console.log(`[prepare-backend] runtime-manifest.json written: ${JSON.stringify(manifest)}`);
};

const readPackageVersion = () => {
  const pyproject = readFileSync(path.join(projectRoot, 'pyproject.toml'), 'utf8');
  const m = /^version\s*=\s*"([^"]+)"/m.exec(pyproject);
  return m ? m[1] : 'unknown';
};

const main = async () => {
  const runtimeRoot = ensureCpythonRuntime();
  installMarginaliaInto(runtimeRoot);
  await copyRuntimeToBundleResources(runtimeRoot);
  console.log('[prepare-backend] done.');
};

main().catch((error) => {
  console.error(error?.stack || String(error));
  process.exit(1);
});
