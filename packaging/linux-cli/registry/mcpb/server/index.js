#!/usr/bin/env node
'use strict'

const fs = require('node:fs')
const path = require('node:path')
const { spawnSync } = require('node:child_process')

function command (directory, roots, platform = process.platform, arch = process.arch, glibc = process.report.getReport().header.glibcVersionRuntime) {
  if (platform !== 'linux' || arch !== 'x64') throw new Error('UFO CLI bundle supports Linux x86_64 only.')
  const version = String(glibc || '').match(/^(\d+)\.(\d+)/)
  if (!version || Number(version[1]) < 2 || (Number(version[1]) === 2 && Number(version[2]) < 35)) throw new Error('UFO CLI requires glibc 2.35 or later. Alpine/musl is unsupported.')
  if (!roots.length) throw new Error('select at least one existing absolute allowed directory. No default access is granted.')
  for (const root of roots) {
    if (!path.isAbsolute(root) || !fs.statSync(root).isDirectory()) throw new Error('every allowed root must be an existing absolute directory.')
  }
  return { binary: path.join(directory, 'vendor/universal-file-opener-1.6.0-linux-x64/bin/ufo'), args: ['mcp', ...roots.flatMap(root => ['--allow-root', root])] }
}

if (require.main === module) {
  try {
    const invocation = command(path.resolve(__dirname, '..'), process.argv.slice(2))
    const result = spawnSync(invocation.binary, invocation.args, { stdio: 'inherit' })
    if (result.error) throw result.error
    if (result.signal) process.kill(process.pid, result.signal)
    else process.exitCode = result.status === null ? 1 : result.status
  } catch (error) {
    process.stderr.write(`UFO CLI MCP bundle: ${error.message}\n`)
    process.exitCode = 2
  }
}

module.exports = { command }
