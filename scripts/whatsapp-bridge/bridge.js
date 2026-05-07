#!/usr/bin/env node
/**
 * Hermes Agent WhatsApp Bridge
 *
 * Standalone Node.js process that connects to WhatsApp via Baileys
 * and exposes HTTP endpoints for the Python gateway adapter.
 *
 * Endpoints (matches gateway/platforms/whatsapp.py expectations):
 *   GET  /messages       - Long-poll for new incoming messages
 *   POST /send           - Send a message { chatId, message, replyTo? }
 *   POST /edit           - Edit a sent message { chatId, messageId, message }
 *   POST /send-media     - Send media natively { chatId, filePath, mediaType?, caption?, fileName? }
 *   POST /typing         - Send typing indicator { chatId }
 *   GET  /chat/:id       - Get chat info
 *   GET  /health         - Health check
 *
 * Usage:
 *   node bridge.js --port 3000 --session ~/.hermes/whatsapp/session
 */

import { makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, downloadMediaMessage } from '@whiskeysockets/baileys';
import express from 'express';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import path from 'path';
import { mkdirSync, readFileSync, writeFileSync, existsSync, readdirSync, unlinkSync } from 'fs';
import { createHash, randomBytes, timingSafeEqual } from 'crypto';
import { execSync, spawn } from 'child_process';
import { tmpdir } from 'os';
import qrcode from 'qrcode-terminal';
import { matchesAllowedUser, parseAllowedUsers } from './allowlist.js';
import { createChipManagerBridge } from './chip-manager.js';

// Parse CLI args
const args = process.argv.slice(2);
function getArg(name, defaultVal) {
  const idx = args.indexOf(`--${name}`);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : defaultVal;
}

const WHATSAPP_DEBUG =
  typeof process !== 'undefined' &&
  process.env &&
  typeof process.env.WHATSAPP_DEBUG === 'string' &&
  ['1', 'true', 'yes', 'on'].includes(process.env.WHATSAPP_DEBUG.toLowerCase());

const PORT = parseInt(getArg('port', '3030'), 10);
const SESSION_DIR = getArg('session', path.join(process.env.HOME || '~', '.hermes', 'whatsapp', 'session'));
const IMAGE_CACHE_DIR = path.join(process.env.HOME || '~', '.hermes', 'image_cache');
const DOCUMENT_CACHE_DIR = path.join(process.env.HOME || '~', '.hermes', 'document_cache');
const AUDIO_CACHE_DIR = path.join(process.env.HOME || '~', '.hermes', 'audio_cache');
const PAIR_ONLY = args.includes('--pair-only');
const WHATSAPP_MODE = getArg('mode', process.env.WHATSAPP_MODE || 'self-chat'); // "bot" or "self-chat"
const ALLOWED_USERS = parseAllowedUsers(process.env.WHATSAPP_ALLOWED_USERS || '');
// SPEC-060: whitelist de grupo (alem do gate de @mention)
const ALLOWED_GROUPS = new Set(
  String(process.env.WHATSAPP_ALLOWED_GROUPS || '')
    .split(',').map(s => s.trim()).filter(Boolean)
);
const TRAFEGO_ACK_URL = (process.env.TRAFEGO_ACK_URL || 'http://127.0.0.1:8642/api/trafego/hermes-ack').trim();
const TRAFEGO_ACK_TOKEN = (process.env.HERMES_INBOUND_TOKEN || process.env.FOFO_INBOUND_TOKEN || '').trim();
const TRAFEGO_ACK_STAFF = new Set(
  String(process.env.TRAFEGO_ACK_ALLOWED_PHONES || '5551989150954,5551980148849,5551991987972,5551991518441,5551984580681,5551992559674')
    .split(',').map(s => s.replace(/\D/g, '')).filter(Boolean)
);
const TRAFEGO_ACK_COMMAND_RE = /\b(?:vi|ack|assumi|assumido)\b|\bvou\s+agora\b|\bj[aá]\s+t[oô]\s+em\b|\bsilenci(?:a|ar)\b/i;
const TRAFEGO_ACK_CARD_RE = /(?:#\s*|\bcard\s+)(\d+)\b/i;
// SPEC-060: modo tradutor (grupo especifico + regex palavras-chave)
const TRANSLATOR_GROUP = (process.env.WHATSAPP_TRANSLATOR_GROUP || '').trim();
const TRANSLATOR_REGEX = /(@?hermes\s+(traduz|explica|nos ajude entender|explica ai|me ajude)|\btraduz\s+ai\b)/i;
const TRANSLATOR_PROMPT_PREFIX = `[MODO TRADUTOR T1 — EXECUTE A TRADUCAO, NAO ENSINE COMO TRADUZIR. Voce esta no grupo da mentoria Revolucao T1 (grupo de advogadas/alunas comecando em IA) e um aluno te mencionou. Sua tarefa: responder em portugues de forma SIMPLES MAS COM SUBSTANCIA. SEMPRE ancore a resposta num motivo concreto/tecnico real, nao fique so na analogia. Estrutura recomendada (5-10 linhas): (1) 1 frase direta com o motivo real em linguagem simples (ex: "X e melhor porque Y mantem o contexto inteiro em um lugar so"), (2) 1 analogia curta fora de tech pra ancorar, (3) 1 frase fechando com o impacto pratico ("por isso da menos retrabalho", "por isso o agente sai mais pronto", etc). Pode explicar conceitos tecnicos com palavras simples, mas NUNCA abra mao da substancia — a pessoa quer entender o POR QUE de verdade, nao so uma metafora bonita. Se jargao for inevitavel, explica em parenteses ("skill = receita que o agente segue"). REGRA DE CONTEXTO: se o aluno pedir pra traduzir o que OUTRA pessoa disse ("explica o que X quis dizer", "o que o fulano falou") e voce NAO tem a fala dessa pessoa no contexto atual, responda pedindo pra ele citar/marcar em resposta a mensagem especifica (reply no WhatsApp). NAO invente o que a pessoa disse. NAO responda com template/exemplo generico. PROIBIDO mencionar: outros agentes internos (Clara, Benicio, Larissinha, Mordomo, Fofoqueiro, Bia, Helena, Bebela), SPECs, precos de produtos internos (TAG, 25K, 100K, Revolucao, planos), nomes de mentoradas, infraestrutura, chips, APIs, credenciais. Pergunta do aluno:]\n\n`;

// SPEC-060 (Opcao B): busca historico direto do Evolution DB via docker exec psql
// - Fonte: Postgres container advogando_postgres, db=evolution, tabela "Message"
// - Extrai texto de conversation / extendedTextMessage / image/video caption
// - Roda em qualquer grupo (basta estar em WHATSAPP_ALLOWED_GROUPS)
const TRANSLATOR_HISTORY_COUNT = 15;       // quantas msgs trazer
const TRANSLATOR_HISTORY_WINDOW_SEC = 86400; // janela de 24h
const PSQL_DOCKER_CONTAINER = process.env.EVOLUTION_PG_CONTAINER || 'advogando_postgres';
const PSQL_DB               = process.env.EVOLUTION_PG_DB        || 'evolution';
const PSQL_USER             = process.env.EVOLUTION_PG_USER      || 'postgres';
function escapeSqlLiteral(s) { return String(s).replace(/'/g, "''"); }
function fetchGroupHistory(chatId, count = TRANSLATOR_HISTORY_COUNT, windowSec = TRANSLATOR_HISTORY_WINDOW_SEC) {
  return new Promise((resolve) => {
    const safeChatId = escapeSqlLiteral(chatId);
    const query = `
      SELECT sender, body, ts, from_me FROM (
        SELECT DISTINCT ON ("key"->>'id')
          "key"->>'id' AS wa_id,
          COALESCE("pushName", 'desconhecido') AS sender,
          COALESCE(
            "message"->>'conversation',
            "message"->'extendedTextMessage'->>'text',
            "message"->'imageMessage'->>'caption',
            "message"->'videoMessage'->>'caption',
            ''
          ) AS body,
          "messageTimestamp" AS ts,
          ("key"->>'fromMe')::boolean AS from_me
        FROM "Message"
        WHERE "key"->>'remoteJid' = '${safeChatId}'
          AND "messageTimestamp" > (EXTRACT(EPOCH FROM NOW())::int - ${Number(windowSec)})
          AND "messageType" IN ('conversation', 'extendedTextMessage', 'imageMessage', 'videoMessage')
      ) s
      WHERE body IS NOT NULL AND length(trim(body)) > 0
      ORDER BY ts DESC
      LIMIT ${Number(count)};
    `;
    const proc = spawn('docker', [
      'exec', '-i', PSQL_DOCKER_CONTAINER,
      'psql', '-U', PSQL_USER, '-d', PSQL_DB,
      '-t', '-A', '-F', '\t', '-c', query,
    ], { timeout: 5000 });
    let out = '', err = '';
    proc.stdout.on('data', d => out += d.toString());
    proc.stderr.on('data', d => err += d.toString());
    proc.on('close', () => {
      if (err && !out) { console.error('[bridge] psql err:', err.trim().slice(0, 200)); return resolve([]); }
      const rows = out.split('\n').filter(Boolean).map(line => {
        const [sender, body, ts, fromMe] = line.split('\t');
        return { sender, body: String(body || '').slice(0, 500), ts: Number(ts) || 0, fromMe: fromMe === 't' };
      }).filter(r => r.body && r.body.trim().length > 0);
      rows.reverse(); // ordem cronologica
      resolve(rows);
    });
    proc.on('error', e => { console.error('[bridge] spawn psql:', e.message); resolve([]); });
  });
}
function formatGroupContextRows(rows) {
  if (!rows || rows.length === 0) return '';
  return rows.map(r => `[${r.sender}] ${r.body}`).join('\n');
}

const DEFAULT_REPLY_PREFIX = '⚕ *Hermes Agent*\n────────────\n';
const REPLY_PREFIX = process.env.WHATSAPP_REPLY_PREFIX === undefined
  ? DEFAULT_REPLY_PREFIX
  : process.env.WHATSAPP_REPLY_PREFIX.replace(/\\n/g, '\n');
const MAX_MESSAGE_LENGTH = parseInt(process.env.WHATSAPP_MAX_MESSAGE_LENGTH || '4096', 10);
const CHUNK_DELAY_MS = parseInt(process.env.WHATSAPP_CHUNK_DELAY_MS || '300', 10);

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function formatOutgoingMessage(message) {
  // In bot mode, messages come from a different number so the prefix is
  // redundant — the sender identity is already clear.  Only prepend in
  // self-chat mode where bot and user share the same number.
  if (WHATSAPP_MODE !== 'self-chat') return message;
  return REPLY_PREFIX ? `${REPLY_PREFIX}${message}` : message;
}

function splitLongMessage(message, maxLength = MAX_MESSAGE_LENGTH) {
  const text = String(message || '');
  if (!text) return [];
  if (!Number.isFinite(maxLength) || maxLength < 1 || text.length <= maxLength) {
    return [text];
  }

  const chunks = [];
  let remaining = text;
  while (remaining.length > maxLength) {
    let splitAt = remaining.lastIndexOf('\n', maxLength);
    if (splitAt < Math.floor(maxLength / 2)) {
      splitAt = remaining.lastIndexOf(' ', maxLength);
    }
    if (splitAt < 1) splitAt = maxLength;

    chunks.push(remaining.slice(0, splitAt).trimEnd());
    remaining = remaining.slice(splitAt).trimStart();
  }
  if (remaining) chunks.push(remaining);
  return chunks;
}

function trackSentMessageId(sent) {
  if (sent?.key?.id) {
    recentlySentIds.add(sent.key.id);
    if (recentlySentIds.size > MAX_RECENT_IDS) {
      recentlySentIds.delete(recentlySentIds.values().next().value);
    }
  }
}

function normalizeWhatsAppId(value) {
  if (!value) return '';
  return String(value).replace(':', '@');
}

function getMessageContent(msg) {
  const content = msg?.message || {};
  if (content.ephemeralMessage?.message) return content.ephemeralMessage.message;
  if (content.viewOnceMessage?.message) return content.viewOnceMessage.message;
  if (content.viewOnceMessageV2?.message) return content.viewOnceMessageV2.message;
  if (content.documentWithCaptionMessage?.message) return content.documentWithCaptionMessage.message;
  if (content.templateMessage?.hydratedTemplate) return content.templateMessage.hydratedTemplate;
  if (content.buttonsMessage) return content.buttonsMessage;
  if (content.listMessage) return content.listMessage;
  return content;
}

function getContextInfo(messageContent) {
  if (!messageContent || typeof messageContent !== 'object') return {};
  for (const value of Object.values(messageContent)) {
    if (value && typeof value === 'object' && value.contextInfo) {
      return value.contextInfo;
    }
  }
  return {};
}

function extractQuotedText(messageContent) {
  const quoted = getContextInfo(messageContent)?.quotedMessage;
  if (!quoted || typeof quoted !== 'object') return '';
  return quoted.conversation || quoted.extendedTextMessage?.text || quoted.imageMessage?.caption || quoted.videoMessage?.caption || '';
}

function extractPlainText(messageContent) {
  if (!messageContent || typeof messageContent !== 'object') return '';
  return messageContent.conversation
    || messageContent.extendedTextMessage?.text
    || messageContent.imageMessage?.caption
    || messageContent.videoMessage?.caption
    || messageContent.documentMessage?.caption
    || '';
}

mkdirSync(SESSION_DIR, { recursive: true });

// Build LID → phone reverse map from session files (lid-mapping-{phone}.json)
function buildLidMap() {
  const map = {};
  try {
    for (const f of readdirSync(SESSION_DIR)) {
      const m = f.match(/^lid-mapping-(\d+)\.json$/);
      if (!m) continue;
      const phone = m[1];
      const lid = JSON.parse(readFileSync(path.join(SESSION_DIR, f), 'utf8'));
      if (lid) map[String(lid)] = phone;
    }
  } catch {}
  return map;
}
let lidToPhone = buildLidMap();

function senderPhoneFromId(senderId) {
  const raw = String(senderId || '').replace(/@.*/, '').replace(/:.*$/, '');
  const mapped = lidToPhone[raw] || raw;
  return String(mapped || '').replace(/\D/g, '');
}

async function maybeHandleTrafegoAck({ body, senderId, senderName }) {
  if (!TRAFEGO_ACK_TOKEN || !body || !TRAFEGO_ACK_COMMAND_RE.test(body) || !TRAFEGO_ACK_CARD_RE.test(body)) {
    return false;
  }

  const senderPhone = senderPhoneFromId(senderId);
  if (!TRAFEGO_ACK_STAFF.has(senderPhone)) {
    return false;
  }

  try {
    const response = await fetch(TRAFEGO_ACK_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Hermes-Token': TRAFEGO_ACK_TOKEN,
      },
      body: JSON.stringify({
        text: body,
        senderPhone,
        senderId,
        senderName,
      }),
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => '');
      console.warn(`[SPEC-110] trafego ack webhook failed status=${response.status} detail=${detail.slice(0, 200)}`);
      return false;
    }
    console.log(`[SPEC-110] trafego ack webhook accepted sender=${senderPhone}`);
    return true;
  } catch (error) {
    console.warn(`[SPEC-110] trafego ack webhook error: ${error.message}`);
    return false;
  }
}

const logger = pino({ level: 'warn' });

// Message queue for polling
const messageQueue = [];
const MAX_QUEUE_SIZE = 100;

// Track recently sent message IDs to prevent echo-back loops with media
const recentlySentIds = new Set();
const MAX_RECENT_IDS = 50;

let sock = null;
let connectionState = 'disconnected';

const chipManagerBridge = createChipManagerBridge({
  botId: 'hermes',
  logger: console,
  getMeta: () => ({ connection_state: connectionState }),
});
chipManagerBridge.startHeartbeat();

async function startSocket() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: state,
    logger,
    printQRInTerminal: false,
    browser: ['Hermes Agent', 'Chrome', '120.0'],
    syncFullHistory: false,
    markOnlineOnConnect: false,
    // Required for Baileys 7.x: without this, incoming messages that need
    // E2EE session re-establishment are silently dropped (msg.message === null)
    getMessage: async (key) => {
      // We don't maintain a message store, so return a placeholder.
      // This is enough for Baileys to complete the retry handshake.
      return { conversation: '' };
    },
  });

  sock.ev.on('creds.update', () => { saveCreds(); lidToPhone = buildLidMap(); });

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      console.log('\n📱 Scan this QR code with WhatsApp on your phone:\n');
      qrcode.generate(qr, { small: true });
      console.log('\nWaiting for scan...\n');
    }

    if (connection === 'close') {
      const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
      connectionState = 'disconnected';

      if (reason === DisconnectReason.loggedOut) {
        console.log('❌ Logged out. Delete session and restart to re-authenticate.');
        process.exit(1);
      } else {
        // 515 = restart requested (common after pairing). Always reconnect.
        if (reason === 515) {
          console.log('↻ WhatsApp requested restart (code 515). Reconnecting...');
        } else {
          console.log(`⚠️  Connection closed (reason: ${reason}). Reconnecting in 3s...`);
        }
        setTimeout(startSocket, reason === 515 ? 1000 : 3000);
      }
    } else if (connection === 'open') {
      connectionState = 'connected';
      console.log('✅ WhatsApp connected!');
      if (PAIR_ONLY) {
        console.log('✅ Pairing complete. Credentials saved.');
        // Give Baileys a moment to flush creds, then exit cleanly
        setTimeout(() => process.exit(0), 2000);
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    // In self-chat mode, your own messages commonly arrive as 'append' rather
    // than 'notify'. Accept both and filter agent echo-backs below.
    if (type !== 'notify' && type !== 'append') return;

    const botIds = Array.from(new Set([
      normalizeWhatsAppId(sock.user?.id),
      normalizeWhatsAppId(sock.user?.lid),
    ].filter(Boolean)));

    for (const msg of messages) {
      if (!msg.message) continue;

      const chatId = msg.key.remoteJid;
      if (WHATSAPP_DEBUG) {
        try {
          console.log(JSON.stringify({
            event: 'upsert', type,
            fromMe: !!msg.key.fromMe, chatId,
            senderId: msg.key.participant || chatId,
            messageKeys: Object.keys(msg.message || {}),
          }));
        } catch {}
      }
      const senderId = msg.key.participant || chatId;
      const isGroup = chatId.endsWith('@g.us');
      const senderNumber = senderId.replace(/@.*/, '');

      // Handle fromMe messages based on mode
      if (msg.key.fromMe) {
        if (isGroup || chatId.includes('status')) continue;

        if (WHATSAPP_MODE === 'bot') {
          // Bot mode: separate number. ALL fromMe are echo-backs of our own replies — skip.
          continue;
        }

        // Self-chat mode: only allow messages in the user's own self-chat
        // WhatsApp now uses LID (Linked Identity Device) format: 67427329167522@lid
        // AND classic format: 34652029134@s.whatsapp.net
        // sock.user has both: { id: "number:10@s.whatsapp.net", lid: "lid_number:10@lid" }
        const myNumber = (sock.user?.id || '').replace(/:.*@/, '@').replace(/@.*/, '');
        const myLid = (sock.user?.lid || '').replace(/:.*@/, '@').replace(/@.*/, '');
        const chatNumber = chatId.replace(/@.*/, '');
        const isSelfChat = (myNumber && chatNumber === myNumber) || (myLid && chatNumber === myLid);
        if (!isSelfChat) continue;
      }

      // Handle !fromMe messages (from other people) based on mode.
      // Self-chat mode only responds to the user's own messages to
      // themselves — stranger DMs / group pings must never reach the
      // Python gateway, otherwise a pairing-code reply fires in response
      // to arbitrary incoming messages (#8389).
      if (!msg.key.fromMe) {
        if (WHATSAPP_MODE === 'self-chat') {
          try {
            console.log(JSON.stringify({
              event: 'ignored',
              reason: 'self_chat_mode_rejects_non_self',
              chatId,
              senderId,
            }));
          } catch {}
          continue;
        }

        // Skip group messages UNLESS Hermes is @mentioned.
        // Group msgs from allowed users (e.g. Vinicius) were leaking through
        // because the old isGroup filter was inside the fromMe block only.
        // Fix: ignore groups by default, but allow when @mentioned by JID or LID.
        if (isGroup) {
          const messageContent_ = getMessageContent(msg);
          const contextInfo_ = getContextInfo(messageContent_);
          const mentions = (contextInfo_?.mentionedJid || []).map(j => String(j).replace(/:.*@/, '@').replace(/@.*/, ''));
          const myNumber = (sock.user?.id || '').replace(/:.*@/, '@').replace(/@.*/, '');
          const myLid = (sock.user?.lid || '').replace(/:.*@/, '@').replace(/@.*/, '');
          const isMentioned = mentions.some(m => (myNumber && m === myNumber) || (myLid && m === myLid));

          if (!isMentioned) {
            if (WHATSAPP_DEBUG) console.log(`[GROUP-SKIP] No @mention, ignoring group msg from ${senderNumber} in ${chatId}`);
            continue;
          }
          if (WHATSAPP_DEBUG) console.log(`[GROUP-MENTION] @mentioned in group ${chatId} by ${senderNumber}, processing`);
        }

        // SPEC-060: se WHATSAPP_ALLOWED_GROUPS estiver definida, so libera grupos listados.
        // DM nao e afetada. Se variavel vazia, comportamento original (libera todos grupos mencionados).
        if (isGroup && ALLOWED_GROUPS.size > 0 && !ALLOWED_GROUPS.has(chatId)) {
          if (WHATSAPP_DEBUG) console.log(`[GROUP-BLOCK] ${chatId} not in ALLOWED_GROUPS, dropping`);
          continue;
        }

        if (!isGroup) {
          const ackBody = extractPlainText(getMessageContent(msg));
          const acked = await maybeHandleTrafegoAck({
            body: ackBody,
            senderId,
            senderName: msg.pushName || senderNumber,
          });
          if (acked) {
            continue;
          }
        }

        // Check allowlist for messages from others (resolve LID ↔ phone aliases)
        if (!matchesAllowedUser(senderId, ALLOWED_USERS, SESSION_DIR)) {
          try {
            console.log(JSON.stringify({
              event: 'ignored',
              reason: 'allowlist_mismatch',
              chatId,
              senderId,
            }));
          } catch {}
          continue;
        }
      }

      const messageContent = getMessageContent(msg);
      const contextInfo = getContextInfo(messageContent);
      const mentionedIds = Array.from(new Set((contextInfo?.mentionedJid || []).map(normalizeWhatsAppId).filter(Boolean)));
      const quotedParticipant = normalizeWhatsAppId(contextInfo?.participant || contextInfo?.remoteJid || '');

      // Extract message body
      let body = '';
      let hasMedia = false;
      let mediaType = '';
      const mediaUrls = [];

      if (messageContent.conversation) {
        body = messageContent.conversation;
      } else if (messageContent.extendedTextMessage?.text) {
        body = messageContent.extendedTextMessage.text;
      } else if (messageContent.imageMessage) {
        body = messageContent.imageMessage.caption || '';
        hasMedia = true;
        mediaType = 'image';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = messageContent.imageMessage.mimetype || 'image/jpeg';
          const extMap = { 'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif' };
          const ext = extMap[mime] || '.jpg';
          mkdirSync(IMAGE_CACHE_DIR, { recursive: true });
          const filePath = path.join(IMAGE_CACHE_DIR, `img_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download image:', err.message);
        }
      } else if (messageContent.videoMessage) {
        body = messageContent.videoMessage.caption || '';
        hasMedia = true;
        mediaType = 'video';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = messageContent.videoMessage.mimetype || 'video/mp4';
          const ext = mime.includes('mp4') ? '.mp4' : '.mkv';
          mkdirSync(DOCUMENT_CACHE_DIR, { recursive: true });
          const filePath = path.join(DOCUMENT_CACHE_DIR, `vid_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download video:', err.message);
        }
      } else if (messageContent.audioMessage || messageContent.pttMessage) {
        hasMedia = true;
        mediaType = messageContent.pttMessage ? 'ptt' : 'audio';
        try {
          const audioMsg = messageContent.pttMessage || messageContent.audioMessage;
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = audioMsg.mimetype || 'audio/ogg';
          const ext = mime.includes('ogg') ? '.ogg' : mime.includes('mp4') ? '.m4a' : '.ogg';
          mkdirSync(AUDIO_CACHE_DIR, { recursive: true });
          const filePath = path.join(AUDIO_CACHE_DIR, `aud_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download audio:', err.message);
        }
      } else if (messageContent.documentMessage) {
        body = messageContent.documentMessage.caption || '';
        hasMedia = true;
        mediaType = 'document';
        const fileName = messageContent.documentMessage.fileName || 'document';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          mkdirSync(DOCUMENT_CACHE_DIR, { recursive: true });
          const safeFileName = path.basename(fileName).replace(/[^a-zA-Z0-9._-]/g, '_');
          const filePath = path.join(DOCUMENT_CACHE_DIR, `doc_${randomBytes(6).toString('hex')}_${safeFileName}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download document:', err.message);
        }
      }

      // For media without caption, use a placeholder so the API message is never empty
      if (hasMedia && !body) {
        body = `[${mediaType} received]`;
      }

      // Ignore Hermes' own reply messages in self-chat mode to avoid loops.
      if (msg.key.fromMe && ((REPLY_PREFIX && body.startsWith(REPLY_PREFIX)) || recentlySentIds.has(msg.key.id))) {
        if (WHATSAPP_DEBUG) {
          try { console.log(JSON.stringify({ event: 'ignored', reason: 'agent_echo', chatId, messageId: msg.key.id })); } catch {}
        }
        continue;
      }

      // Skip empty messages
      if (!body && !hasMedia) {
        if (WHATSAPP_DEBUG) {
          try { 
            console.log(JSON.stringify({ event: 'ignored', reason: 'empty', chatId, messageKeys: Object.keys(msg.message || {}) })); 
          } catch (err) {
            console.error('Failed to log empty message event:', err);
          }
        }
        continue;
      }

      if (!msg.key.fromMe && !isGroup) {
        const acked = await chipManagerBridge.maybeAckIncomingAlert({
          chatId,
          text: body,
          quotedText: extractQuotedText(messageContent),
          senderPhone: senderNumber,
        });
        if (acked) {
          continue;
        }
      }

      // SPEC-060: injeta diretriz de tradutor se no grupo configurado + regex bate
      if (isGroup && ALLOWED_GROUPS.has(chatId)) {
        const rows = await fetchGroupHistory(chatId);
        const history = formatGroupContextRows(rows);
        const historyBlock = history
          ? `\n\n--- ULTIMAS ${rows.length} MENSAGENS DO GRUPO (contexto real vindo do DB; NAO repita nem traduza uma por uma, use para entender do que estao falando) ---\n${history}\n--- FIM HISTORICO ---\n\n`
          : '';
        if (WHATSAPP_DEBUG) console.log(`[TRANSLATOR-MODE] chatId=${chatId} prefix + ${rows.length} msgs do DB`);
        body = TRANSLATOR_PROMPT_PREFIX + historyBlock + body;
      }

      const event = {
        messageId: msg.key.id,
        chatId,
        senderId,
        senderName: msg.pushName || senderNumber,
        chatName: isGroup ? (chatId.split('@')[0]) : (msg.pushName || senderNumber),
        isGroup,
        body,
        hasMedia,
        mediaType,
        mediaUrls,
        mentionedIds,
        quotedParticipant,
        botIds,
        timestamp: msg.messageTimestamp,
      };

      messageQueue.push(event);
      if (messageQueue.length > MAX_QUEUE_SIZE) {
        messageQueue.shift();
      }
    }
  });
}


function hasValidBackupWebhookSecret(providedSecret) {
  const expectedSecret = process.env.HERMES_SHARED_SECRET || '';
  if (!providedSecret || !expectedSecret) return false;

  const providedDigest = createHash('sha256').update(String(providedSecret)).digest();
  const expectedDigest = createHash('sha256').update(expectedSecret).digest();
  return timingSafeEqual(providedDigest, expectedDigest);
}

async function sendTextMessage(chatId, message) {
  const sent = await sock.sendMessage(chatId, {
    text: formatOutgoingMessage(message),
    linkPreview: null,
  });
  chipManagerBridge.rememberOutgoingAlert(chatId, message);

  if (sent?.key?.id) {
    recentlySentIds.add(sent.key.id);
    if (recentlySentIds.size > MAX_RECENT_IDS) {
      recentlySentIds.delete(recentlySentIds.values().next().value);
    }
  }

  return sent;
}

// HTTP server
const app = express();
app.use(express.json());

// Poll for new messages (long-poll style)
app.get('/messages', (req, res) => {
  const msgs = messageQueue.splice(0, messageQueue.length);
  res.json(msgs);
});

// Send a message
app.post('/send', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, message, replyTo } = req.body;
  if (!chatId || !message) {
    return res.status(400).json({ error: 'chatId and message are required' });
  }

  try {
    const chunks = splitLongMessage(message);
    const messageIds = [];
    for (let i = 0; i < chunks.length; i += 1) {
      const sent = await sendTextMessage(chatId, chunks[i]);
      if (sent?.key?.id) messageIds.push(sent.key.id);
      if (chunks.length > 1 && i < chunks.length - 1) {
        await sleep(CHUNK_DELAY_MS);
      }
    }

    res.json({
      success: true,
      messageId: messageIds[messageIds.length - 1],
      messageIds,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});


app.post('/api/webhook/backup-alert', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const providedSecret = req.headers['x-hermes-secret'];
  if (!hasValidBackupWebhookSecret(providedSecret)) {
    return res.status(401).json({ error: 'unauthorized' });
  }

  const { source, severity, message, host } = req.body || {};
  if (!source || !severity || !message || !host) {
    return res.status(400).json({ error: 'source, severity, message, and host are required' });
  }

  const emoji = severity === 'critical' ? '🚨' : '⚠️';
  const text = `${emoji} *Backup alert* (${source})
Host: ${host}
Severity: ${severity}

${String(message).slice(0, 3500)}`;

  const results = await Promise.allSettled([
    sendTextMessage('5551991987972@s.whatsapp.net', text),
    sendTextMessage('5551984213925@s.whatsapp.net', text),
  ]);

  const delivered = results.filter(result => result.status === 'fulfilled').length;
  const failed = results.filter(result => result.status === 'rejected');
  console.log('[backup-alert] delivery summary', { source, severity, delivered, failed: failed.length });
  if (failed.length > 0) {
    console.error('[backup-alert] partial delivery failure', {
      source,
      severity,
      failed: failed.map(result => result.reason?.message || String(result.reason)),
    });
  }

  res.json({ ok: true, delivered, failed: failed.length });
});

// Edit a previously sent message
app.post('/edit', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, messageId, message } = req.body;
  if (!chatId || !messageId || !message) {
    return res.status(400).json({ error: 'chatId, messageId, and message are required' });
  }

  try {
    const key = { id: messageId, fromMe: true, remoteJid: chatId };
    const chunks = splitLongMessage(message);
    const messageIds = [];

    await sock.sendMessage(chatId, {
      text: formatOutgoingMessage(chunks[0]),
      edit: key,
      linkPreview: null,
    });
    if (chunks.length > 1) {
      for (let i = 1; i < chunks.length; i += 1) {
        const sent = await sendTextMessage(chatId, chunks[i]);
        if (sent?.key?.id) messageIds.push(sent.key.id);
        if (i < chunks.length - 1) {
          await sleep(CHUNK_DELAY_MS);
        }
      }
    }

    res.json({ success: true, messageIds });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// MIME type map and media type inference for /send-media
const MIME_MAP = {
  jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
  webp: 'image/webp', gif: 'image/gif',
  mp4: 'video/mp4', mov: 'video/quicktime', avi: 'video/x-msvideo',
  mkv: 'video/x-matroska', '3gp': 'video/3gpp',
  pdf: 'application/pdf',
  doc: 'application/msword',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
};

function inferMediaType(ext) {
  if (['jpg', 'jpeg', 'png', 'webp', 'gif'].includes(ext)) return 'image';
  if (['mp4', 'mov', 'avi', 'mkv', '3gp'].includes(ext)) return 'video';
  if (['ogg', 'opus', 'mp3', 'wav', 'm4a'].includes(ext)) return 'audio';
  return 'document';
}

// Send media (image, video, document) natively
app.post('/send-media', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, filePath, mediaType, caption, fileName } = req.body;
  if (!chatId || !filePath) {
    return res.status(400).json({ error: 'chatId and filePath are required' });
  }

  try {
    if (!existsSync(filePath)) {
      return res.status(404).json({ error: `File not found: ${filePath}` });
    }

    const buffer = readFileSync(filePath);
    const ext = filePath.toLowerCase().split('.').pop();
    const type = mediaType || inferMediaType(ext);
    let msgPayload;

    switch (type) {
      case 'image':
        msgPayload = { image: buffer, caption: caption || undefined, mimetype: MIME_MAP[ext] || 'image/jpeg' };
        break;
      case 'video':
        msgPayload = { video: buffer, caption: caption || undefined, mimetype: MIME_MAP[ext] || 'video/mp4' };
        break;
      case 'audio': {
        // WhatsApp only renders a native voice bubble (ptt) when the file is ogg/opus.
        // If the caller passes mp3, wav, m4a etc. (e.g. from Edge TTS / NeuTTS),
        // silently convert to ogg/opus via ffmpeg so ptt is always honoured.
        let audioBuffer = buffer;
        let audioExt = ext;
        const needsConversion = !['ogg', 'opus'].includes(ext);
        let tmpPath = null;
        if (needsConversion) {
          tmpPath = path.join(tmpdir(), `hermes_voice_${randomBytes(6).toString('hex')}.ogg`);
          try {
            execSync(
              `ffmpeg -y -i ${JSON.stringify(filePath)} -ar 48000 -ac 1 -c:a libopus ${JSON.stringify(tmpPath)}`,
              { timeout: 30000, stdio: 'pipe' }
            );
            audioBuffer = readFileSync(tmpPath);
            audioExt = 'ogg';
          } catch (convErr) {
            // ffmpeg not available or conversion failed — fall back to original format
            console.warn('[bridge] ffmpeg conversion failed, sending as file attachment:', convErr.message);
          } finally {
            try { if (tmpPath && existsSync(tmpPath)) unlinkSync(tmpPath); } catch (_) {}
          }
        }
        const audioMime = (audioExt === 'ogg' || audioExt === 'opus') ? 'audio/ogg; codecs=opus' : 'audio/mpeg';
        msgPayload = { audio: audioBuffer, mimetype: audioMime, ptt: audioExt === 'ogg' || audioExt === 'opus' };
        break;
      }
      case 'document':
      default:
        msgPayload = {
          document: buffer,
          fileName: fileName || path.basename(filePath),
          caption: caption || undefined,
          mimetype: MIME_MAP[ext] || 'application/octet-stream',
        };
        break;
    }

    const sent = await sock.sendMessage(chatId, msgPayload);

    trackSentMessageId(sent);

    res.json({ success: true, messageId: sent?.key?.id });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Typing indicator
app.post('/typing', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected' });
  }

  const { chatId } = req.body;
  if (!chatId) return res.status(400).json({ error: 'chatId required' });

  try {
    await sock.sendPresenceUpdate('composing', chatId);
    res.json({ success: true });
  } catch (err) {
    res.json({ success: false });
  }
});

// Chat info
app.get('/chat/:id', async (req, res) => {
  const chatId = req.params.id;
  const isGroup = chatId.endsWith('@g.us');

  if (isGroup && sock) {
    try {
      const metadata = await sock.groupMetadata(chatId);
      return res.json({
        name: metadata.subject,
        isGroup: true,
        participants: metadata.participants.map(p => p.id),
      });
    } catch {
      // Fall through to default
    }
  }

  res.json({
    name: chatId.replace(/@.*/, ''),
    isGroup,
    participants: [],
  });
});

// Health check
app.get('/health', (req, res) => {
  res.json({
    status: connectionState,
    queueLength: messageQueue.length,
    uptime: process.uptime(),
  });
});

// Start
if (PAIR_ONLY) {
  // Pair-only mode: just connect, show QR, save creds, exit. No HTTP server.
  console.log('📱 WhatsApp pairing mode');
  console.log(`📁 Session: ${SESSION_DIR}`);
  console.log();
  startSocket();
} else {
  app.listen(PORT, '127.0.0.1', () => {
    console.log(`🌉 WhatsApp bridge listening on port ${PORT} (mode: ${WHATSAPP_MODE})`);
    console.log(`📁 Session stored in: ${SESSION_DIR}`);
    if (ALLOWED_USERS.size > 0) {
      console.log(`🔒 Allowed users: ${Array.from(ALLOWED_USERS).join(', ')}`);
    } else if (WHATSAPP_MODE === 'self-chat') {
      console.log(`🔒 Self-chat mode — only your own messages to yourself are processed.`);
    } else {
      console.log(`🔒 No WHATSAPP_ALLOWED_USERS set — incoming messages are rejected.`);
      console.log(`   Set WHATSAPP_ALLOWED_USERS=<phone> to authorize specific users,`);
      console.log(`   or WHATSAPP_ALLOWED_USERS=* for an explicit open bot.`);
    }
    console.log();
    startSocket();
  });
}
