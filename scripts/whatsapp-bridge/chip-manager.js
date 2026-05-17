const ALERT_REF_REGEX = /\[chip-alert\s+incident=(\d+)\s+chip=([a-z0-9._-]+)\s+level=(L[1-4])\]/i;

function createNoopLogger() {
  return {
    info() {},
    warn() {},
    error() {},
  };
}

function normalizeBaseUrl(value) {
  if (typeof value !== 'string') {
    return null;
  }

  const trimmed = value.trim();
  return trimmed ? trimmed.replace(/\/+$/, '') : null;
}

export function parseAlertRef(text) {
  if (typeof text !== 'string') {
    return null;
  }

  const match = text.match(ALERT_REF_REGEX);
  if (!match) {
    return null;
  }

  return {
    incidentId: Number.parseInt(match[1], 10),
    chipId: match[2],
    level: match[3],
  };
}

export function createChipManagerBridge({ botId, logger = createNoopLogger(), getMeta = () => ({}) }) {
  const baseUrl = normalizeBaseUrl(process.env.CHIP_MANAGER_URL || '');
  const apiKey = String(process.env.CHIP_MANAGER_KEY || '').trim();
  const heartbeatMs = Number.parseInt(process.env.CHIP_MANAGER_HEARTBEAT_MS || '60000', 10);
  const pendingByChat = new Map();
  let warnedDisabled = false;

  function isConfigured() {
    return Boolean(baseUrl && apiKey);
  }

  function warnDisabledOnce() {
    if (warnedDisabled) {
      return;
    }

    warnedDisabled = true;
    logger.warn('chip_manager_bridge_disabled', {
      bot_id: botId,
      has_url: Boolean(baseUrl),
      has_key: Boolean(apiKey),
    });
  }

  async function postJson(path, payload) {
    if (!isConfigured()) {
      warnDisabledOnce();
      return null;
    }

    const response = await fetch(`${baseUrl}${path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': apiKey,
      },
      body: JSON.stringify(payload || {}),
    });

    if (!response.ok) {
      const body = await response.text().catch(() => response.statusText || 'request_failed');
      throw new Error(`chip_manager_http_${response.status}:${body.slice(0, 160)}`);
    }

    return response.json().catch(() => null);
  }

  function rememberOutgoingAlert(chatId, text) {
    const ref = parseAlertRef(text);
    if (!ref || !chatId) {
      return null;
    }

    pendingByChat.set(chatId, {
      ...ref,
      seenAt: Date.now(),
    });

    return ref;
  }

  function resolveAckRef({ chatId, text, quotedText }) {
    return parseAlertRef(text) || parseAlertRef(quotedText) || pendingByChat.get(chatId) || null;
  }

  async function maybeAckIncomingAlert({ chatId, text = '', quotedText = '', senderPhone = null }) {
    const ref = resolveAckRef({ chatId, text, quotedText });
    if (!ref) {
      return false;
    }

    try {
      await postJson(`/chip/${encodeURIComponent(ref.chipId)}/ack`, {
        incident_id: ref.incidentId,
        resolution: 'acked_by_user',
        source: `${botId}_reply`,
        sender_phone: senderPhone,
      });
      pendingByChat.delete(chatId);
      logger.info('chip_manager_alert_acked', {
        bot_id: botId,
        chip_id: ref.chipId,
        incident_id: ref.incidentId,
        level: ref.level,
        sender_phone: senderPhone,
      });
      return true;
    } catch (error) {
      logger.error('chip_manager_ack_failed', {
        bot_id: botId,
        chip_id: ref.chipId,
        incident_id: ref.incidentId,
        error: error.message,
      });
      return false;
    }
  }

  function startHeartbeat() {
    if (!isConfigured()) {
      warnDisabledOnce();
      return () => {};
    }

    const tick = async () => {
      try {
        await postJson('/heartbeat', {
          bot_id: botId,
          ts: new Date().toISOString(),
          meta: getMeta() || {},
        });
      } catch (error) {
        logger.error('chip_manager_heartbeat_failed', {
          bot_id: botId,
          error: error.message,
        });
      }
    };

    void tick();
    const timer = setInterval(() => {
      void tick();
    }, heartbeatMs);

    if (typeof timer.unref === 'function') {
      timer.unref();
    }

    return () => clearInterval(timer);
  }

  return {
    maybeAckIncomingAlert,
    rememberOutgoingAlert,
    startHeartbeat,
  };
}
