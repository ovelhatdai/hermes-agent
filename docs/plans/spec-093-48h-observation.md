# SPEC-093 Sandbox Observation - 48h

Runbook unico para acompanhar a SPEC-093 no sandbox sem fingir que o gate fecha sozinho.

## 1. Ownership e regra de honestidade

- O script `python3 scripts/smoke_asaas_sandbox.py` prepara a validacao basica do runtime e tenta autocarregar os env files padrao do Hermes.
- A observacao de 48h continua sendo operator-owned.
- Nao marque a SPEC-093 como validada so porque o smoke passou uma vez.
- Nao marque a SPEC-093 como validada sem evidencias reais de cobrancas, webhooks e ausencia de regressao durante a janela.

## 2. O que significa verde no sandbox

### Verde de preflight

Todos os itens abaixo precisam estar verdes antes de abrir a janela de 48h:

1. `python3 scripts/smoke_asaas_sandbox.py` retorna `Veredito: PASS`.
2. `curl -i -X POST https://central.advogando100k.com.br/asaas/webhook ...` retorna `401` do Hermes, nao `404` nem `405`.
3. `systemctl status hermes-gateway.service --no-pager` mostra o service ativo apos a ultima mudanca.
4. `ASAAS_WEBHOOK_TOKEN`, `ASAAS_API_KEY`, `HERMES_GATEWAY_TOKEN` e os precos por SKU estao carregados no ambiente do service.

### Verde de observacao 48h

O sandbox so pode ser considerado verde para a SPEC-093 se, ao final da janela:

1. Houve pelo menos 10 cobrancas sandbox criadas sem erro para o fluxo da Clara/Hermes.
2. Houve pelo menos 10 webhooks recebidos e persistidos sem `5xx` nem loop de retry visivel.
3. Nao existem rows antigas em `asaas_event_log` com `processed_at IS NULL` sem justificativa operacional.
4. Nao existem regresses obvias no Hermes (`/health` verde, sem traceback repetido de `asaas` ou `zapsign`).
5. Qualquer evento suportado que chegou no sandbox teve resultado rastreavel em banco e logs.

Passar no smoke sem passar nesses cinco pontos significa apenas "preflight pronto", nao "gate fechado".

## 3. Sequencia recomendada

### T0 - antes de abrir a janela

```bash
cd /root/.hermes/hermes-agent
python3 scripts/smoke_asaas_sandbox.py
curl -i -X POST https://central.advogando100k.com.br/asaas/webhook \
  -H "Content-Type: application/json" \
  -d "{\"id\":\"evt_public_probe\",\"event\":\"PAYMENT_CREATED\",\"payment\":{\"id\":\"pay_public_probe\",\"value\":100,\"customer\":\"cus_public_probe\"}}"
systemctl status hermes-gateway.service --no-pager
```

Se o probe publico voltar `404` ou `405`, a janela ainda nao pode abrir.

### T+1h / T+24h / T+48h

Repetir os checks de logs e SQL abaixo, registrando evidencias objetivas em um handoff operacional.

## 4. Logs para monitorar

### Journal do service

```bash
journalctl -u hermes-gateway -n 200 --no-pager | rg "asaas|zapsign"
```

Sinais esperados:

- entradas de webhook autenticado
- processamento de eventos ou sweep
- ausencia de traceback repetido

Sinais ruins:

- `404` ou `405` no webhook publico
- `invalid_asaas_access_token` quando o painel deveria estar usando o token certo
- excecoes recorrentes em `asaas_router` ou `asaas_events`
- erro repetido de `zapsign_not_configured` quando o ambiente deveria estar completo

## 5. Queries SQL uteis

### 5.1 Volume por tipo de evento

```bash
psql "$HERMES_MEDIA_DISPATCH_DATABASE_URL" -P pager=off -c "
SELECT event_type, count(*)
FROM asaas_event_log
GROUP BY 1
ORDER BY 2 DESC;"
```

### 5.2 Pendencias nao processadas

```bash
psql "$HERMES_MEDIA_DISPATCH_DATABASE_URL" -P pager=off -c "
SELECT id, asaas_event_id, event_type, payment_id, created_at, error_message
FROM asaas_event_log
WHERE processed_at IS NULL
ORDER BY created_at ASC
LIMIT 20;"
```

### 5.3 Cruzamento de cobranca e evento

```bash
psql "$HERMES_MEDIA_DISPATCH_DATABASE_URL" -P pager=off -c "
SELECT
  pr.created_at AS payment_created_at,
  pr.sku,
  pr.status AS payment_status,
  pr.asaas_payment_id,
  ev.event_type,
  ev.processed_at,
  ev.error_message
FROM asaas_payment_request pr
LEFT JOIN asaas_event_log ev
  ON ev.payment_id = pr.asaas_payment_id
ORDER BY pr.created_at DESC
LIMIT 20;"
```

## 6. Sinais de regressao

- `python3 scripts/smoke_asaas_sandbox.py` deixa de retornar `Veredito: PASS`.
- `/health` sai de `200` ou o webhook volta a responder `404` local ou publicamente.
- `asaas_event_log` acumula pendencias sem sweep recuperar.
- `asaas_payment_request.status` fica parado em `requested` ou `created` sem justificativa operacional.
- o service reinicia em loop ou perde envs.

Qualquer item acima invalida a janela corrente e exige nova avaliacao antes de seguir para producao.

## 7. Criterios de rollback

Acione rollback se qualquer um destes pontos ocorrer:

1. webhook publico quebra (`404`, `405` ou timeout) apos deploy ou restart
2. webhook autenticado comeca a gerar `error_logged=true` de forma repetida
3. rows pendentes aumentam continuamente e o sweep nao recupera
4. create-payment sandbox para de responder com `invoice_url` ou `payment_id`

Rollback operacional minimo:

1. pausar novos testes no painel Asaas
2. revisar envs e reiniciar `hermes-gateway.service`
3. se a regressao veio de codigo recente, reverter apenas o patch da SPEC-093 no repo `/root/.hermes/hermes-agent`
4. repetir smoke local e probe publico antes de retomar

## 8. Checklist final do operador

- Smoke local passou e o resumo foi anexado ao handoff.
- Probe publico retornou `401` do Hermes antes do cadastro no painel.
- Logs do `hermes-gateway` foram revisados em T0, T+1h, T+24h e T+48h.
- As queries SQL foram registradas com evidencias de volume, processamento e ausencia de backlog sem dono.
- A decisao final de "sandbox verde" foi baseada em evidencias reais, nao em inferencia do Codex.
