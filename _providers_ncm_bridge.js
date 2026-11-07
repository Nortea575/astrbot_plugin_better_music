'use strict'

const crypto = require('node:crypto')
const fs = require('node:fs')
const readline = require('node:readline')
const path = require('node:path')
const util = require('node:util')

const PRESET_KEY = '0CoJUm6Qyw8W8jud'
const IV = '0102030405060708'
const PUBLIC_MODULUS = '00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7'

function modPow(base, exponent, modulus) {
  let result = 1n
  base %= modulus
  while (exponent > 0n) {
    if (exponent & 1n) result = (result * base) % modulus
    exponent >>= 1n
    base = (base * base) % modulus
  }
  return result
}

function aesEncrypt(text, key) {
  const cipher = crypto.createCipheriv('aes-128-cbc', key, IV)
  return Buffer.concat([cipher.update(text, 'utf8'), cipher.final()]).toString('base64')
}

function weapiEncrypt(data, secretKey = crypto.randomBytes(8).toString('hex')) {
  if (typeof secretKey !== 'string' || !/^[a-f0-9]{16}$/i.test(secretKey)) {
    throw new Error('Invalid weapi secret key')
  }
  const text = JSON.stringify(data)
  const params = aesEncrypt(aesEncrypt(text, PRESET_KEY), secretKey)
  const reversedKey = secretKey.split('').reverse().join('')
  const reversedKeyHex = Buffer.from(reversedKey, 'utf8').toString('hex')
  const encrypted = modPow(BigInt(`0x${reversedKeyHex}`), 65537n, BigInt(`0x${PUBLIC_MODULUS}`))
    .toString(16)
    .padStart(256, '0')
  return { params, encSecKey: encrypted }
}

for (const method of ['log', 'info', 'debug', 'warn', 'error']) {
  console[method] = (...args) => process.stderr.write(`${util.format(...args)}\n`)
}

const allowed = new Set([
  'login_qr_key',
  'login_qr_create',
  'login_qr_check',
  'login_status',
  'user_account',
  'vip_info',
  'cloudsearch',
  'song_detail',
  'song_url_v1',
  'search_hot',
])

let xeapiInitialization

async function ensureXeapiKey(packageMain, cookie) {
  if (xeapiInitialization) return xeapiInitialization
  xeapiInitialization = (async () => {
    const packageDir = path.dirname(packageMain)
    const keyPath = path.resolve(require('node:os').tmpdir(), 'xeapi_public_key')
    const { getXeapiPublicKey } = require(path.join(packageDir, 'util', 'xeapiKey'))
    const { cookieToJson, generateDeviceId } = require(path.join(packageDir, 'util', 'index'))
    let currentKey = {}
    try {
      currentKey = JSON.parse(fs.readFileSync(keyPath, 'utf8'))
    } catch (_) {}

    const cookieObject = typeof cookie === 'string' ? cookieToJson(cookie) : (cookie || {})
    const deviceId = cookieObject.deviceId || currentKey.deviceId || generateDeviceId()
    global.deviceId = deviceId
    const publicKey = await getXeapiPublicKey(currentKey, deviceId)
    if (!publicKey || !publicKey.sk) {
      throw new Error('xeapi public key initialization failed')
    }
    fs.writeFileSync(keyPath, JSON.stringify(publicKey), { mode: 0o600 })
  })()
  try {
    await xeapiInitialization
  } catch (error) {
    xeapiInitialization = null
    throw error
  }
}

function runBridge() {
  const packageMain = process.argv[2]
  const api = require(packageMain)
  const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity })

  input.on('line', async (line) => {
    let request
    try {
      request = JSON.parse(line)
      const params = { ...(request.params || {}) }
      let value
      if (request.method === 'weapi_encrypt') {
        value = weapiEncrypt(params.data)
      } else {
        if (!allowed.has(request.method) || typeof api[request.method] !== 'function') {
          throw new Error('Unsupported Netease API method')
        }
        if (request.method === 'song_url_v1') {
          await ensureXeapiKey(packageMain, params.cookie)
          params.unblock = 'false'
        }
        value = await api[request.method](params)
      }
      process.stdout.write(`${JSON.stringify({ id: request.id, ok: true, value })}\n`)
    } catch (error) {
      process.stdout.write(
        `${JSON.stringify({ id: request && request.id, ok: false, error: safeError(error, request && request.params && request.params.cookie) })}\n`,
      )
    }
  })
}

function safeError(error, cookie) {
  let message = String(error && error.message ? error.message : error)
  if (typeof cookie === 'string' && cookie) {
    message = message.split(cookie).join('[已隐藏]')
  }
  message = message.replace(/(cookie|token|musickey|access_token|refresh_token)\s*[:=]\s*[^\s,;]+/gi, '$1=[已隐藏]')
  message = message.replace(/(MUSIC_U|MUSIC_A|__csrf|csrf_token|sid|userId|userid)\s*[:=]\s*[^\s,;]+/gi, '$1=[已隐藏]')
  return message.slice(0, 500)
}

module.exports = { weapiEncrypt }
if (require.main === module) runBridge()
