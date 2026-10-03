#!/usr/bin/env node
'use strict'

const fs = require('node:fs')
const path = require('node:path')
const https = require('node:https')
const { spawnSync } = require('node:child_process')
const { readRelease, sha256, verifyImage, verifyVendor, externalLauncher } = require('../lib/runtime')

const DOWNLOAD_TIMEOUT_MS = 120000
const EXTRACTION_TIMEOUT_MS = 120000

function download (url, destination, sizeBytes, redirects = 0, deadline = Date.now() + DOWNLOAD_TIMEOUT_MS) {
  if (new URL(url).protocol !== 'https:' || redirects > 5) throw new Error('download requires HTTPS and at most five redirects.')
  const remaining = deadline - Date.now()
  if (remaining <= 0) throw new Error('release download exceeded its total deadline. Nothing was installed.')
  return new Promise((resolve, reject) => {
    let request
    let response
    let out
    let timer
    let settled = false
    function finish (error, result) {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (error) {
        if (request) request.destroy()
        if (response) response.destroy()
        if (out) out.destroy()
        reject(error)
      } else resolve(result)
    }
    request = https.get(url, { headers: { 'user-agent': 'ufo-cli-npm-wrapper' } }, incoming => {
      response = incoming
      if ([301, 302, 303, 307, 308].includes(response.statusCode)) {
        response.resume()
        if (!response.headers.location) return finish(new Error('download redirect has no location.'))
        try {
          download(new URL(response.headers.location, url).toString(), destination, sizeBytes, redirects + 1, deadline)
            .then(result => finish(null, result), finish)
        } catch (error) { finish(error) }
        return
      }
      if (response.statusCode !== 200) {
        response.resume()
        finish(new Error(`download returned HTTP ${response.statusCode}. Nothing was installed.`))
        return
      }
      out = fs.createWriteStream(destination, { flags: 'wx', mode: 0o600 })
      let received = 0
      response.on('data', data => {
        received += data.length
        if (received > sizeBytes) response.destroy(new Error('download exceeds the release pin size.'))
      })
      response.on('error', finish)
      out.on('error', finish)
      response.pipe(out)
      out.on('finish', () => out.close(error => finish(error, destination)))
    })
    request.on('error', finish)
    request.setTimeout(Math.min(remaining, DOWNLOAD_TIMEOUT_MS), () => finish(new Error('release download timed out. Nothing was installed.')))
    timer = setTimeout(() => finish(new Error('release download exceeded its total deadline. Nothing was installed.')), remaining)
  })
}

function tar (args, timeoutMs = EXTRACTION_TIMEOUT_MS) {
  const result = spawnSync('tar', args, { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, timeout: timeoutMs, killSignal: 'SIGKILL' })
  if (result.error && result.error.code === 'ETIMEDOUT') throw new Error('archive listing or extraction exceeded its two-minute deadline. Nothing was installed.')
  if (result.error || result.status !== 0) throw new Error('GNU tar could not read the release asset. Install GNU tar or use the Debian package.')
  return result.stdout
}

function extract (archive, into, release) {
  const members = tar(['--list', '--gzip', '--file', archive, '--quoting-style=literal']).trimEnd().split('\n')
  if (!members.length || members.some(name =>
    !/^[A-Za-z0-9 ._+/-]+$/.test(name) ||
    !name.startsWith(`${release.image}/`) ||
    name.split('/').some(part => part === '..' || part === '.'))) {
    throw new Error('release archive contains an unexpected path. Nothing was installed.')
  }
  const listing = tar(['--list', '--verbose', '--gzip', '--file', archive, '--quoting-style=literal'])
  if (listing.trimEnd().split('\n').some(line => !['-', 'd'].includes(line[0]))) {
    throw new Error('release archive contains links or special files. Nothing was installed.')
  }
  tar(['--extract', '--gzip', '--file', archive, '--directory', into, '--no-same-owner', '--no-same-permissions'])
}

async function install (root, env = process.env, verifyOnly = false) {
  const release = readRelease(root)
  const vendor = path.join(root, 'vendor')
  const marker = path.join(root, '.use-external-ufo.json')
  if (env.UFO_CLI_SKIP_DOWNLOAD === '1' || (!fs.existsSync(vendor) && fs.existsSync(marker))) {
    const binary = externalLauncher(root, release, env)
    if (!verifyOnly) fs.writeFileSync(marker, JSON.stringify({ version: release.version, binary }) + '\n', { mode: 0o600 })
    process.stderr.write(`@krauq/ufo-cli: using external UFO ${release.version} at ${binary}.\n`)
    return binary
  }
  if (fs.existsSync(vendor)) {
    const binary = verifyVendor(root, release)
    process.stderr.write(`@krauq/ufo-cli: verified existing UFO ${release.version}; no download needed.\n`)
    return binary
  }
  if (verifyOnly) {
    throw new Error('no installed runtime to verify. Enable npm install scripts and reinstall.')
  }

  // Lock and stage beside vendor. Only a complete verified image is renamed
  // into place; a failed download/extraction leaves no installed runtime.
  const lock = path.join(root, '.ufo-install.lock')
  let lockFd
  let staging
  let createdVendor = false
  try {
    try { lockFd = fs.openSync(lock, 'wx', 0o600) } catch (error) {
      if (error.code === 'EEXIST') throw new Error('another install is active, or an interrupted install left .ufo-install.lock. Remove that lock only after confirming no install is running.')
      throw error
    }
    if (fs.existsSync(vendor)) return verifyVendor(root, release)
    staging = fs.mkdtempSync(path.join(root, '.ufo-stage-'))
    const archive = path.join(staging, 'release.tar.gz')
    if (env.UFO_CLI_LOCAL_ASSET) {
      const source = fs.statSync(env.UFO_CLI_LOCAL_ASSET)
      if (!source.isFile() || source.size !== release.asset.sizeBytes) throw new Error('local asset size does not match the release pin. Nothing was installed.')
      fs.copyFileSync(env.UFO_CLI_LOCAL_ASSET, archive, fs.constants.COPYFILE_EXCL)
    } else await download(release.asset.url, archive, release.asset.sizeBytes)
    if (fs.statSync(archive).size !== release.asset.sizeBytes || sha256(archive) !== release.asset.sha256) throw new Error('release size or SHA-256 mismatch. Nothing was installed.')
    extract(archive, staging, release)
    const image = path.join(staging, release.image)
    verifyImage(image, release)
    fs.mkdirSync(vendor)
    createdVendor = true
    fs.renameSync(image, path.join(vendor, release.image))
    createdVendor = false
    fs.rmSync(path.join(root, '.use-external-ufo.json'), { force: true })
    process.stderr.write(`@krauq/ufo-cli: installed verified UFO ${release.version}. Installation needs no system Java; document processing is local.\n`)
    return path.join(vendor, release.asset.launcher)
  } finally {
    if (createdVendor && fs.readdirSync(vendor).length === 0) fs.rmdirSync(vendor)
    if (staging) fs.rmSync(staging, { recursive: true, force: true })
    if (lockFd !== undefined) {
      fs.closeSync(lockFd)
      fs.rmSync(lock, { force: true })
    }
  }
}

if (require.main === module) {
  install(path.resolve(__dirname, '..'), process.env, process.argv.includes('--verify-only')).catch(error => {
    process.stderr.write(`@krauq/ufo-cli: ${error.message}\n`)
    process.exitCode = 1
  })
}

module.exports = { install, download, extract, tar }
