#!/usr/bin/env node
'use strict'

const path = require('node:path')
const { spawnSync } = require('node:child_process')
const { readRelease, launcher, wrapperEnvironment } = require('../lib/runtime')

try {
  const root = path.resolve(__dirname, '..')
  const release = readRelease(root)
  const env = wrapperEnvironment(__filename)
  const result = spawnSync(launcher(root, release), process.argv.slice(2), { stdio: 'inherit', env })
  if (result.error) throw result.error
  if (result.signal) process.kill(process.pid, result.signal)
  else process.exitCode = result.status === null ? 1 : result.status
} catch (error) {
  process.stderr.write(`@krauq/ufo-cli: ${error.message}\n`)
  process.exitCode = 2
}
