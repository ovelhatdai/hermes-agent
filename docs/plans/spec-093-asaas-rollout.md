# SPEC-093 Asaas Rollout Pack

Runbook operacional para a parte manual da SPEC-093. Este documento cobre apenas o que depende de painel Asaas, secrets, validacao operacional e rollback.

## 1. Objetivo

Separar claramente:

- codigo e testes: ja executados no runtime Hermes
- painel Asaas, envs, restart, smoke e observacao: responsabilidade do operador

## 2. Go / No-Go antes de cadastrar webhook

Nao cadastre o webhook no painel antes de estes checks passarem no VPS:

```bash
curl -sS http://127.0.0.1:8642/health
```

Esperado:

```json
{"status": "ok", "platform": "hermes-agent"}
```

```bash
curl -sS -X POST http://127.0.0.1:8642/api/gateway/asaas/create-payment \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Esperado:

```json
{"ok": false, "error": "missing_bearer"}
```

```bash
curl -sS -X POST http://127.0.0.1:8642/asaas/webhook \
  -H 'Content-Type: application/json' \
  -d '{"id":"evt_preflight","event":"PAYMENT_CREATED","payment":{"id":"pay_preflight","value":100,"customer":"cus_preflight"}}'
```

Esperado:

```json
{"ok": false, "error": "missing_asaas_access_token"}
```

```bash
curl -i -X POST https://central.advogando100k.com.br/asaas/webhook \
  -H 'Content-Type: application/json' \
  -d '{"id":"evt_preflight_public","event":"PAYMENT_CREATED","payment":{"id":"pay_preflight_public","value":100,"customer":"cus_preflight_public"}}'
```

Esperado: `HTTP/1.1 401 Unauthorized` com JSON do Hermes, nao `404` e nao `405`.

Se qualquer check acima falhar:

- pare o cadastro no painel Asaas
- confirme que o deploy da SPEC-093 esta no repo real `/root/.hermes/hermes-agent`
- rode `systemctl restart hermes-gateway.service`
- repita o preflight antes de ativar o webhook

Nota: `https://central.advogando100k.com.br/health` nao e o health do Hermes. Use `http://127.0.0.1:8642/health` no VPS.

## 3. Checklist de env

Arquivo de referencia sem secrets: `docs/plans/spec-093-asaas-env.example`

Variaveis obrigatorias para esta rollout:

- `ASAAS_BASE_URL`
- `ASAAS_API_KEY`
- `ASAAS_WEBHOOK_TOKEN`
- `ASAAS_PRICE_TAG_SOLO`
- `ASAAS_PRICE_TAG_DUPLA`
- `ASAAS_PRICE_REVOLUCAO_BASICO`
- `ASAAS_PRICE_REVOLUCAO_AVANCADO`
- `ZAPSIGN_TEMPLATE_TAG_SOLO`
- `ZAPSIGN_TEMPLATE_TAG_DUPLA`
- `ZAPSIGN_TEMPLATE_REVOLUCAO_BASICO`
- `ZAPSIGN_TEMPLATE_REVOLUCAO_AVANCADO`
- `HERMES_NOTIFY_PHONE_VINI`
- `HERMES_NOTIFY_PHONE_JOANNE`

Defaults usados pelo runtime e recomendados no mesmo env:

- `ASAAS_DEFAULT_DUE_DAYS=3`
- `ASAAS_DEFAULT_MAX_INSTALLMENTS=12`

Onde o service ja le envs hoje:

- `/root/.hermes/.env`
- `/etc/hermes/media-dispatch.env`
- `/etc/hermes/advogandodash.env`

Service file:

- `hermes-gateway.service`

## 4. Cadastro manual do webhook no painel Asaas

### Sandbox

- Nome sugerido: `SPEC-093 Hermes Clara SDR Sandbox`
- URL: `https://central.advogando100k.com.br/asaas/webhook`
- Token: exatamente o mesmo valor de `ASAAS_WEBHOOK_TOKEN`
- Eventos a marcar:
  - `PAYMENT_RECEIVED`
  - `PAYMENT_OVERDUE`
  - `PAYMENT_CHARGEBACK_REQUESTED`
  - `PAYMENT_CREDIT_CARD_CAPTURE_REFUSED`
  - `PAYMENT_BANK_SLIP_VIEWED`
  - `PAYMENT_REFUNDED`

### Producao

- Nome sugerido: `SPEC-093 Hermes Clara SDR Producao`
- URL: `https://central.advogando100k.com.br/asaas/webhook`
- Token: exatamente o mesmo valor de `ASAAS_WEBHOOK_TOKEN` do ambiente de producao
- Eventos a marcar: exatamente a mesma lista do sandbox

### Observacoes sandbox vs producao

- Nunca reutilize `ASAAS_API_KEY` de sandbox em producao.
- Nunca reutilize `ASAAS_WEBHOOK_TOKEN` de sandbox em producao.
- Cadastre primeiro no sandbox, execute os smokes abaixo e deixe em observacao antes de virar producao.
- So cadastre o webhook se o preflight do item 2 estiver verde, principalmente o `POST` publico em `https://central.advogando100k.com.br/asaas/webhook`.

## 5. Comandos operacionais copy/paste

### 5.1 Carregar env do service no shell atual

```bash
set -a
source /root/.hermes/.env
[ -f /etc/hermes/media-dispatch.env ] && source /etc/hermes/media-dispatch.env
[ -f /etc/hermes/advogandodash.env ] && source /etc/hermes/advogandodash.env
set +a
```

### 5.2 Health do Hermes

```bash
curl -sS http://127.0.0.1:8642/health
systemctl status hermes-gateway.service --no-pager
```

### 5.3 Smoke do create-payment

Atencao: rode isto primeiro com `ASAAS_BASE_URL` apontando para sandbox. Em producao, este comando cria uma cobranca real.

```bash
set -a
source /root/.hermes/.env
[ -f /etc/hermes/media-dispatch.env ] && source /etc/hermes/media-dispatch.env
[ -f /etc/hermes/advogandodash.env ] && source /etc/hermes/advogandodash.env
set +a

STAMP="$(date +%s)"
PAYLOAD=$(cat <<JSON
{
  "sku": "TAG_DUPLA",
  "customer": {
    "name": "Smoke SPEC093 ${STAMP}",
    "cpfCnpj": "12345678900",
    "email": "spec093.${STAMP}@example.com",
    "phone": "5511999998888"
  },
  "agent": "clara_sdr",
  "conv_id": "spec093-${STAMP}",
  "lead_phone": "5511999998888",
  "installments": 12
}
JSON
)

curl -sS -X POST http://127.0.0.1:8642/api/gateway/asaas/create-payment \
  -H "Authorization: Bearer ${HERMES_GATEWAY_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD"
```

Esperado: JSON com `invoice_url`, `payment_id`, `due_date` e `asaas_customer_id`.

### 5.4 Smoke do webhook sem token

```bash
curl -sS -X POST http://127.0.0.1:8642/asaas/webhook \
  -H 'Content-Type: application/json' \
  -d '{"id":"evt_smoke_missing_token","event":"PAYMENT_CREATED","payment":{"id":"pay_smoke_missing_token","value":100,"customer":"cus_smoke_missing_token"}}'
```

Esperado:

```json
{"ok": false, "error": "missing_asaas_access_token"}
```

### 5.5 Smoke do webhook com token valido

Usa `PAYMENT_CREATED` de proposito para nao disparar notificacoes ou ZapSign. O evento deve entrar no log e ser marcado como `unsupported_event:PAYMENT_CREATED`.

```bash
set -a
source /root/.hermes/.env
[ -f /etc/hermes/media-dispatch.env ] && source /etc/hermes/media-dispatch.env
[ -f /etc/hermes/advogandodash.env ] && source /etc/hermes/advogandodash.env
set +a

curl -sS -X POST http://127.0.0.1:8642/asaas/webhook \
  -H "asaas-access-token: ${ASAAS_WEBHOOK_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"id":"evt_smoke_valid_token","event":"PAYMENT_CREATED","payment":{"id":"pay_smoke_valid_token","value":100,"customer":"cus_smoke_valid_token"}}'
```

Esperado:

```json
{"ok": true, "duplicate": false}
```

### 5.6 Smoke do webhook duplicate

Rode o mesmo comando de cima outra vez, sem trocar o `id`.

Esperado:

```json
{"ok": true, "duplicate": true}
```

### 5.7 Check do path publico antes de cadastrar no painel Asaas

```bash
curl -i -X POST https://central.advogando100k.com.br/asaas/webhook \
  -H 'Content-Type: application/json' \
  -d '{"id":"evt_public_probe","event":"PAYMENT_CREATED","payment":{"id":"pay_public_probe","value":100,"customer":"cus_public_probe"}}'
```

Esperado: `HTTP/1.1 401 Unauthorized` com JSON do Hermes. Se vier `404` ou `405`, nao cadastrar no painel ainda.

### 5.8 Query SQL de verificacao

```bash
set -a
source /root/.hermes/.env
[ -f /etc/hermes/media-dispatch.env ] && source /etc/hermes/media-dispatch.env
[ -f /etc/hermes/advogandodash.env ] && source /etc/hermes/advogandodash.env
set +a

psql "$HERMES_MEDIA_DISPATCH_DATABASE_URL" -P pager=off -x -c "
SELECT
  pr.created_at AS payment_created_at,
  pr.sku,
  pr.status AS payment_status,
  pr.asaas_payment_id,
  pr.invoice_url,
  ev.created_at AS event_created_at,
  ev.asaas_event_id,
  ev.event_type,
  ev.processed_at,
  ev.notification_sent,
  ev.zapsign_doc_id,
  ev.error_message
FROM public.asaas_payment_request pr
LEFT JOIN public.asaas_event_log ev
  ON ev.payment_id = pr.asaas_payment_id
ORDER BY pr.created_at DESC, ev.created_at DESC
LIMIT 20;
"
```

Query focada nos smokes do webhook:

```bash
set -a
source /root/.hermes/.env
[ -f /etc/hermes/media-dispatch.env ] && source /etc/hermes/media-dispatch.env
[ -f /etc/hermes/advogandodash.env ] && source /etc/hermes/advogandodash.env
set +a

psql "$HERMES_MEDIA_DISPATCH_DATABASE_URL" -P pager=off -x -c "
SELECT
  asaas_event_id,
  event_type,
  processed_at,
  notification_sent,
  error_message,
  created_at
FROM public.asaas_event_log
WHERE asaas_event_id IN ('evt_smoke_valid_token', 'evt_smoke_missing_token', 'evt_public_probe')
ORDER BY created_at DESC;
"
```

## 6. Rollback

### 6.1 Desativar webhook no painel Asaas

- Abra o webhook da SPEC-093 no painel correto do Asaas.
- Desative o webhook ou remova o endpoint `https://central.advogando100k.com.br/asaas/webhook`.
- Nao deixe o webhook apontando para um path que esteja em `404` ou `405`.

### 6.2 Revogar chave sandbox

- Revogue a `ASAAS_API_KEY` sandbox no painel Asaas.
- Gere uma nova chave antes de retomar o rollout.
- Se trocar o token do webhook, atualize `ASAAS_WEBHOOK_TOKEN` no env e reinicie o service.

### 6.3 Reiniciar ou pausar o service Hermes

```bash
systemctl restart hermes-gateway.service
systemctl stop hermes-gateway.service
systemctl start hermes-gateway.service
systemctl status hermes-gateway.service --no-pager
journalctl -u hermes-gateway.service -n 50 --no-pager
```

### 6.4 Reverter commit sem destruir o worktree

Use apenas se houve commit dedicado para a SPEC-093.

```bash
cd /root/.hermes/hermes-agent
git log --oneline --decorate -n 20
git revert <commit_sha>
systemctl restart hermes-gateway.service
```

Nao use `git reset --hard` no runtime.

## 7. Handoff curto para o operador

Sequencia recomendada:

1. Confirmar preflight local e publico do item 2.
2. Preencher envs obrigatorios sem commitar secrets.
3. Reiniciar `hermes-gateway.service`.
4. Rodar smoke de `create-payment` em sandbox.
5. Rodar smoke do webhook sem token, com token e duplicate.
6. Validar SQL.
7. So entao cadastrar o webhook no painel Asaas sandbox.
8. Acompanhar observacao antes de promover para producao.
