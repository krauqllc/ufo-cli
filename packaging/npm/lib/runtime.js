'use strict'

const crypto = require('node:crypto')
const fs = require('node:fs')
const path = require('node:path')
const { spawnSync } = require('node:child_process')

function platformKey (platform = process.platform, arch = process.arch, glibc = process.report.getReport().header.glibcVersionRuntime) {
  if (platform !== 'linux' || arch !== 'x64') {
    throw new Error(`no release for ${platform}/${arch}. UFO CLI currently supports Linux x86_64. Use a supported Linux host. Native macOS and Windows CLI downloads are not yet available.`)
  }
  const version = String(glibc || '').match(/^(\d+)\.(\d+)/)
  if (!version || Number(version[1]) < 2 || (Number(version[1]) === 2 && Number(version[2]) < 35)) {
    throw new Error('UFO CLI requires glibc 2.35 or later. Alpine/musl is unsupported. Ubuntu 24.04 and Debian 12 are tested installation targets.')
  }
  return 'linux-x64'
}

function readRelease (root, platform = process.platform, arch = process.arch) {
  const key = platformKey(platform, arch)
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'))
  const pins = JSON.parse(fs.readFileSync(path.join(root, 'release-pins.json'), 'utf8'))
  const asset = pins.assets && pins.assets[key]
  if (!/^\d+\.\d+\.\d+$/.test(pins.version) || pins.version !== pkg.version || !asset) {
    throw new Error('package version and release pins must name the same exact supported release.')
  }
  for (const field of ['sha256', 'treeSha256', 'launcherSha256']) {
    if (!/^[a-f0-9]{64}$/.test(asset[field]) || /^0+$/.test(asset[field])) {
      throw new Error(`release pin ${field} is missing or is a placeholder. Nothing was installed.`)
    }
  }
  const url = new URL(asset.url)
  const image = `universal-file-opener-${pins.version}-linux-x64`
  if (url.protocol !== 'https:' || url.username || url.password ||
      asset.launcher !== `${image}/bin/ufo` ||
      !Number.isSafeInteger(asset.sizeBytes) || asset.sizeBytes <= 0 ||
      !Number.isSafeInteger(asset.fileCount) || asset.fileCount <= 0) {
    throw new Error('release pins contain an invalid URL, size, file count or launcher path.')
  }
  return { version: pins.version, image, asset }
}

function sha256 (file) {
  const hash = crypto.createHash('sha256')
  const buffer = Buffer.allocUnsafe(1024 * 1024)
  const fd = fs.openSync(file, 'r')
  try {
    let count
    while ((count = fs.readSync(fd, buffer, 0, buffer.length, null)) > 0) hash.update(buffer.subarray(0, count))
  } finally {
    fs.closeSync(fd)
  }
  return hash.digest('hex')
}

// Complete content identity, including executable bits. Update this pin from
// the approved archive, never from a mutable installation receipt.
function treeIdentity (image) {
  const files = []
  function visit (directory) {
    for (const name of fs.readdirSync(directory).sort()) {
      const file = path.join(directory, name)
      const stat = fs.lstatSync(file)
      if (stat.isDirectory()) visit(file)
      else if (stat.isFile()) {
        files.push({ path: path.relative(image, file).split(path.sep).join('/'), sizeBytes: stat.size, sha256: sha256(file), executable: Boolean(stat.mode & 0o111) })
      } else throw new Error('runtime contains a symbolic link or a non-regular file.')
    }
  }
  visit(image)
  files.sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0)
  return { sha256: crypto.createHash('sha256').update(JSON.stringify(files)).digest('hex'), fileCount: files.length }
}

function executable (file) {
  const resolved = fs.realpathSync(file)
  if (!fs.statSync(resolved).isFile()) throw new Error('launcher is not a regular file.')
  fs.accessSync(resolved, fs.constants.X_OK)
  return resolved
}

function verifyImage (image, release) {
  if (!fs.lstatSync(image).isDirectory()) throw new Error('runtime image is not a real directory.')
  const identity = treeIdentity(image)
  if (identity.fileCount !== release.asset.fileCount || identity.sha256 !== release.asset.treeSha256) {
    throw new Error(`installed runtime does not match the complete ${release.version} release. Remove this package deliberately and reinstall; no existing runtime was changed.`)
  }
  return executable(path.join(image, 'bin/ufo'))
}

function verifyVendor (root, release) {
  const vendor = path.join(root, 'vendor')
  if (!fs.lstatSync(vendor).isDirectory() || JSON.stringify(fs.readdirSync(vendor)) !== JSON.stringify([release.image])) {
    throw new Error('vendor must contain only the pinned runtime image. No existing runtime was changed.')
  }
  return verifyImage(path.join(vendor, release.image), release)
}

function wrapperEnvironment (shim, env = process.env) {
  const self = fs.realpathSync(shim)
  let active
  try { active = JSON.parse(env.UFO_CLI_ACTIVE_WRAPPERS || '[]') } catch { active = [] }
  if (!Array.isArray(active) || active.some(item => typeof item !== 'string')) active = []
  if (active.includes(self)) throw new Error('recursive ufo wrapper on PATH. Set UFO_CLI_BINARY to the actual absolute UFO launcher.')
  return { ...env, UFO_CLI_ACTIVE_WRAPPERS: JSON.stringify([...active, self]) }
}

function externalLauncher (root, release, env = process.env) {
  const shim = path.join(root, 'bin/ufo.js')
  const self = fs.realpathSync(shim)
  const childEnv = wrapperEnvironment(shim, env)
  let selected = env.UFO_CLI_BINARY
  const marker = path.join(root, '.use-external-ufo.json')
  if (!selected && fs.existsSync(marker)) {
    let saved
    try { saved = JSON.parse(fs.readFileSync(marker, 'utf8')) } catch {
      throw new Error('saved external launcher configuration is invalid. Reinstall deliberately with UFO_CLI_BINARY set to the matching absolute launcher.')
    }
    if (!saved || saved.version !== release.version ||
        (saved.binary !== undefined && (typeof saved.binary !== 'string' || !path.isAbsolute(saved.binary)))) {
      throw new Error('saved external launcher configuration does not name this release and an absolute launcher. Reinstall deliberately with UFO_CLI_BINARY set.')
    }
    // Older local preparation saved only the version and used PATH. New
    // installations retain the resolved launcher and verify it on every use.
    selected = saved.binary
  }
  let candidates
  if (selected) {
    if (!path.isAbsolute(selected)) throw new Error('UFO_CLI_BINARY must be an absolute launcher path.')
    candidates = [selected]
  } else {
    candidates = (env.PATH || '').split(path.delimiter).map(directory => path.resolve(directory || '.', 'ufo'))
  }
  for (const candidate of candidates) {
    let resolved
    try { resolved = executable(candidate) } catch { continue }
    if (resolved === self) continue
    const result = spawnSync(resolved, ['--version'], { encoding: 'utf8', env: childEnv, timeout: 10000, maxBuffer: 1024 * 1024 })
    if (!result.error && result.status === 0 && result.stdout.trim() === `ufo ${release.version}`) return resolved
    // Respect PATH precedence. Do not silently select a different installed
    // release further down PATH after encountering a real incompatible ufo.
    throw new Error(`external ufo must report "ufo ${release.version}". Set UFO_CLI_BINARY to that release's absolute launcher.`)
  }
  throw new Error(`no external UFO ${release.version} launcher. The npm shim cannot use itself. Install the matching Debian/app-image release or allow the pinned download.`)
}

function launcher (root, release, env = process.env) {
  if (env.UFO_CLI_BINARY) return externalLauncher(root, release, env)
  const installed = path.join(root, 'vendor', release.asset.launcher)
  if (fs.existsSync(installed)) {
    const resolved = executable(installed)
    if (sha256(resolved) !== release.asset.launcherSha256) throw new Error('installed launcher does not match the release pin. Run npm run verify in the package directory.')
    return resolved
  }
  if (fs.existsSync(path.join(root, '.use-external-ufo.json')) || env.UFO_CLI_SKIP_DOWNLOAD === '1') return externalLauncher(root, release, env)
  throw new Error('no installed runtime. Enable npm install scripts and reinstall, or explicitly set UFO_CLI_SKIP_DOWNLOAD=1 with the matching UFO release already installed.')
}

module.exports = { platformKey, readRelease, sha256, treeIdentity, executable, verifyImage, verifyVendor, wrapperEnvironment, externalLauncher, launcher }
