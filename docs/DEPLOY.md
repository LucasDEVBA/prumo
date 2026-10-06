# Deploy do Prumo na AWS

Este guia leva o Prumo do repositório para uma conta AWS com o CDK (Python) em `infra/`.
Tudo é criado por uma única stack, a `PrumoStack`.

## O que a stack cria

| Recurso | Para quê |
|---|---|
| DynamoDB `DecisionsTable` (pk/sk, on-demand, PITR, índice esparso `gsi1`, TTL em `expires_at`) | Decisões, contadores da PPI, eventos e leituras. O `gsi1` é a fila de revisão. A decisão expira em 180 dias ([LGPD](#retenção-de-dados-ttl-de-180-dias)) |
| Lambda `api` (FastAPI via Mangum) atrás de uma **HTTP API** | Classifica leads, recebe rótulos, faz deploy/rollback manual do prompt |
| Lambda `estimator` + **EventBridge Scheduler** de hora em hora | Lê a qualidade, publica a métrica EMF `Prumo/SloBreached` e aplica a regra de rollback (rede de segurança) |
| Alarme `SloBreached` (Maximum ≥ 1 em 2 de 2 horas) + regra do EventBridge | Em `ALARM`, chama a Lambda `rollback` |
| Lambda `rollback` | Caminho rápido: volta o prompt para a versão anterior se as últimas 2 leituras da versão no ar estão abaixo da meta. A troca é compare-and-swap; o estimador aplica a mesma regra a cada hora, então um alarme pulado ou reentregue nunca volta duas versões |
| Fila SQS `RollbackDeadLetters` (SSE-SQS, só TLS, 14 dias) | Evento de rollback que o EventBridge não entregou ou que falhou em todas as tentativas da Lambda |
| Step Functions (Standard) + Lambda `review_task` | Revisão humana com `waitForTaskToken`, prazo de 72 h; falha da `review_task` é repetida 3 vezes (2 s, 4 s, 8 s) |
| AppConfig `prumo` / `prod` / `prompt-version` | Guarda qual versão do prompt está ativa e o histórico. A versão inicial é implantada por um custom resource que só age na criação da stack |
| Secrets Manager `AdminToken` | Token do header `X-Prumo-Token` (gerado pela AWS, ninguém digita). A Lambda recebe só o ARN e lê o valor no cold start |
| Alarmes de vigilância + tópico SNS | Ver [Alarmes](#alarmes) |
| AWS Budgets (opcional) | E-mail em 40% e 100% do limite mensal |

Não há VPC, NAT Gateway nem OpenSearch: todos os serviços usados têm endpoint público com IAM e
TLS, e o custo parado fica em poucos dólares (ver [Custos](#custos-esperados)).

## Pré-requisitos

1. **Conta AWS** com um usuário/SSO de administrador para o bootstrap e a criação do role de CI.
2. **Região com os modelos**: os ids padrão são inference profiles `us.` (inferência entre
   regiões dos EUA). Use `us-east-1`, `us-east-2` ou `us-west-2`. Em outra geografia, troque os
   modelos pelo context `classifierModel`/`judgeModel` (ex.: `eu.amazon.nova-lite-v1:0`).
3. **Acesso aos modelos no Bedrock**:
   - Modelos da Amazon (Nova) ficam disponíveis na primeira chamada.
   - Modelos da Anthropic (o juiz usa Claude Haiku 4.5) exigem que a conta envie, uma única vez,
     o formulário de caso de uso no console do Bedrock.
   - **Faça uma chamada de teste de cada modelo no playground do console, com o usuário
     administrador, antes do primeiro deploy.** A primeira chamada de um modelo de terceiro
     ativa a assinatura do Marketplace; se ela acontecer pela Lambda, o role mínimo dela (sem
     `aws-marketplace:*`) recebe `AccessDenied`.
4. **Ferramentas locais**: Node.js 20+ e npm, Python 3.12 e [uv](https://docs.astral.sh/uv/),
   AWS CLI v2 autenticado. O CLI do CDK tem versão exata em `infra/package.json` (com
   `infra/package-lock.json`): instale com `npm ci` dentro de `infra/` e rode sempre
   `npx --no -- cdk ...`. O `--no` impede o npx de baixar outro pacote se o local não estiver
   instalado. Para atualizar o CLI: `npm install --save-exact aws-cdk@<versão>` em `infra/` e
   commit dos dois arquivos.
5. **Bootstrap do CDK** (uma vez por conta/região, com credencial de administrador):

   ```bash
   cd infra && npm ci
   npx --no -- cdk bootstrap aws://<CONTA>/us-east-1
   ```

   O bootstrap cria os roles `cdk-hnb659fds-*` que o deploy usa. Em conta compartilhada, prefira
   `--cloudformation-execution-policies` com uma policy menor que `AdministratorAccess`.

### Role de deploy para o GitHub Actions (OIDC, sem chave de longa duração)

1. Crie o provedor OIDC do GitHub (uma vez por conta):

   ```bash
   aws iam create-open-id-connect-provider \
     --url https://token.actions.githubusercontent.com \
     --client-id-list sts.amazonaws.com
   ```

2. Crie o role `prumo-github-deploy` com esta **trust policy** (troque `<CONTA>`, `<DONO>` e
   `<REPO>`). O `sub` amarra o role ao environment `prod` do repositório: outro repositório,
   fork ou branch sem esse environment não consegue assumir.

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Principal": {
           "Federated": "arn:aws:iam::<CONTA>:oidc-provider/token.actions.githubusercontent.com"
         },
         "Action": "sts:AssumeRoleWithWebIdentity",
         "Condition": {
           "StringEquals": {
             "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
             "token.actions.githubusercontent.com:sub": "repo:<DONO>/<REPO>:environment:prod"
           }
         }
       }
     ]
   }
   ```

3. Dê ao role **só** a permissão de assumir os roles do bootstrap (quem cria os recursos é o
   role de execução do CloudFormation, não o CI):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "sts:AssumeRole",
         "Resource": "arn:aws:iam::<CONTA>:role/cdk-hnb659fds-*-<CONTA>-<REGIAO>"
       }
     ]
   }
   ```

4. No GitHub, em *Settings → Environments*, crie o environment **`prod`** (recomendado: exigir
   aprovação de um revisor). Configure:

   | Tipo | Nome | Valor |
   |---|---|---|
   | Secret | `AWS_DEPLOY_ROLE_ARN` | ARN do role acima |
   | Variável | `AWS_ACCOUNT_ID` | 12 dígitos da conta (o deploy recusa outra conta) |
   | Variável | `AWS_REGION` | ex.: `us-east-1` (padrão) |
   | Variável (opcional) | `PRUMO_ALERT_EMAIL` | e-mail dos alarmes |
   | Variável (opcional) | `PRUMO_BUDGET_EMAIL` / `PRUMO_BUDGET_USD` | orçamento mensal |
   | Variável (opcional) | `PRUMO_ALLOWED_ORIGINS` | origens CORS, separadas por vírgula |

## Parâmetros (context do CDK)

Passe com `-c chave=valor` no `cdk synth/deploy`. Valor inválido para o synth com mensagem clara.

| Chave | Padrão | Efeito |
|---|---|---|
| `dev` | `false` | `true` = tudo com `DESTROY` (tabela, logs, segredo) e sem proteção de deleção |
| `alertEmail` | — | Assina o e-mail no tópico SNS dos alarmes |
| `budgetEmail` / `budgetUsd` | — / `25` | Cria o AWS Budgets mensal com alertas em 40% e 100% |
| `allowedOrigins` | `http://localhost:8000` | Origens CORS (`https://...` ou `http://localhost`). `*` é recusado |
| `apiRateLimit` / `apiBurstLimit` | `10` / `20` | Throttling do stage da HTTP API (req/s) |
| `classifierModel` / `judgeModel` | Nova Lite / Claude Haiku 4.5 (`us.`) | Modelos usados; o IAM é derivado deles  Os modelos da Anthropic exigem enviar o formulário de caso de uso da conta antes do primeiro uso (senão o Bedrock responde `ResourceNotFoundException`); sem ele, use `-c judgeModel=us.amazon.nova-pro-v1:0`. |
| `bedrockGuardrailId` / `bedrockGuardrailVersion` | — / `DRAFT` | Liga o guardrail e o `bedrock:ApplyGuardrail` |
| `lambdaAssetPath` | `build/lambda` | Pasta do pacote das Lambdas (os testes usam um pacote falso) |

## Deploy

### Pela máquina local

```bash
uv sync --locked                      # dependências do app exatamente como no uv.lock
bash scripts/build_lambda.sh          # gera build/lambda (Linux arm64, Python 3.12) a partir do uv.lock
cd infra
uv sync --locked                      # cria infra/.venv com aws-cdk-lib (lock próprio)
uv run pytest -q                      # invariantes da stack
npm ci                                # CLI do CDK na versão do package-lock.json
npx --no -- cdk diff                  # revise o que vai mudar
npx --no -- cdk deploy -c alertEmail=voce@exemplo.com
```

Para um ambiente descartável, acrescente `-c dev=true`.

**Reprodutibilidade.** O pacote das Lambdas sai do `uv.lock` da raiz (`uv export --locked
--no-dev`), e cada wheel é conferido pelo hash do lock. Lock desatualizado em relação ao
`pyproject.toml` quebra o build; no CI (`CI=true`) o lock é obrigatório. Mudou uma dependência?
Rode `uv lock` na raiz e faça commit do `uv.lock`. O `uvicorn` fica no extra `server` e no grupo
`dev`: o `uv sync` de desenvolvimento já deixa o `prumo serve` pronto, e o pacote da Lambda não
leva o servidor (o build falha se ele aparecer lá).

### Pelo GitHub Actions

*Actions → Deploy → Run workflow* na branch `main`. Antes de qualquer credencial da AWS, o
workflow repete os portões do CI no commit que vai subir: `ruff check`, `ruff format --check`,
`mypy` (app e infra), `pytest` do app (incluindo os testes lentos) e `pytest` da infra. Só então
gera o pacote, instala o CLI do CDK com `npm ci`, assume o role por OIDC e executa
`cdk deploy --require-approval never`. As saídas da stack aparecem no resumo da execução.

Depois do primeiro deploy, **confirme as assinaturas de e-mail** (SNS e Budgets): sem o clique
no link de confirmação, nenhum alerta chega.

## Token administrativo (Secrets Manager)

O header `X-Prumo-Token` é exigido em deploy/rollback do prompt, em `POST /api/v1/decisions`,
`GET /api/v1/review/queue` e `POST /api/v1/review/{id}`. Fora do simulador, sem token
configurado, essas rotas recusam (fail-closed).

**Como é criado.** O segredo `AdminToken` (48 caracteres, sem pontuação) é gerado pelo próprio
Secrets Manager no primeiro deploy: o valor nunca passa pelo código, pelo template, pelo CI nem
por variável de ambiente. A Lambda da API recebe só `PRUMO_ADMIN_TOKEN_SECRET_ARN` e a permissão
`secretsmanager:GetSecretValue` nesse segredo (nenhuma outra Lambda tem); ela lê o valor no cold
start e o guarda em memória. Nada de `PRUMO_ADMIN_TOKEN` na AWS: valor em env var aparece para
quem tem `lambda:GetFunctionConfiguration` e só muda com novo deploy.

**Como ler** (para usar nas chamadas):

```bash
SECRET=$(aws cloudformation describe-stacks --stack-name PrumoStack \
  --query "Stacks[0].Outputs[?OutputKey=='AdminTokenSecretArn'].OutputValue" --output text)
TOKEN=$(aws secretsmanager get-secret-value --secret-id "$SECRET" --query SecretString --output text)
```

**Como rotacionar** (vazamento, saída de alguém do time, ou rotina):

```bash
aws secretsmanager put-secret-value --secret-id "$SECRET" --secret-string \
  "$(aws secretsmanager get-random-password --password-length 48 --exclude-punctuation \
     --query RandomPassword --output text)"

# Ambientes de execução já quentes guardaram o token antigo: force a troca de todos.
FN=$(aws lambda list-functions \
  --query "Functions[?starts_with(FunctionName, 'PrumoStack-ApiFunction')].FunctionName" \
  --output text)
aws lambda update-function-configuration --function-name "$FN" \
  --description "API HTTP do Prumo (token rotacionado em $(date -u +%FT%TZ))"
```

Qualquer atualização de configuração faz a Lambda trocar os ambientes de execução; a descrição é
o campo mais inofensivo (não use `--environment`, que **substitui** todas as variáveis). O
próximo `cdk deploy` devolve a descrição original. Até a troca, ambientes antigos ainda aceitam o
token anterior, então em caso de vazamento rode os dois comandos em sequência. Distribua o token
novo para quem chama a API.

## Retenção de dados (TTL de 180 dias)

O texto do lead é dado pessoal (LGPD). Cada decisão é gravada com `expires_at` (epoch em
segundos) = criação + `PRUMO_DECISION_TTL_DAYS` (180, definido na stack), e o TTL do DynamoDB
apaga o item depois dessa data, sem custo de escrita. Os contadores da estimativa (somas da PPI),
eventos e leituras não guardam texto de lead e não expiram.

- A exclusão pelo TTL não é instantânea: costuma acontecer em até alguns dias depois de vencer.
  Quem lê a tabela deve ignorar itens com `expires_at` no passado se o prazo for rígido.
- O PITR guarda o histórico da tabela por até 35 dias: um item apagado pelo TTL ainda pode voltar
  num restore desse período. Considere isso na resposta a um pedido de eliminação.
- Itens gravados antes de existir o atributo `expires_at` nunca expiram sozinhos; se houver,
  preencha o atributo neles (ou apague-os) uma única vez.
- Mudar o prazo vale só para decisões novas (o `expires_at` é calculado na gravação).

## Alarmes

Todos publicam no tópico SNS `Alerts` (e-mail opcional via `alertEmail`). O tópico exige TLS e
tem um `Allow sns:Publish` para `cloudwatch.amazonaws.com`, preso a alarmes desta conta e região
(`aws:SourceArn` + `aws:SourceAccount`): sem ele, a policy só com o `Deny` do TLS bloquearia o
próprio CloudWatch, e os alarmes mudariam de estado sem avisar ninguém.

| Alarme | Dispara quando | O que fazer |
|---|---|---|
| `SloBreachedAlarm` | 2 de 2 leituras horárias com a faixa abaixo da meta | Nada: o rollback automático age. Confira o log da Lambda `rollback` |
| `EstimatorSilentAlarm` | Nenhuma invocação do estimador em 2 h (dead-man's switch) | Schedule desabilitada ou quebrada; sem leitura não há rollback |
| `EstimatorErrors` | Erro na Lambda do estimador | Log do estimador |
| `RollbackErrors` | Erro na Lambda de rollback | Log do rollback; veja também a DLQ |
| `RollbackDeadLettersAlarm` | Mensagem na fila `RollbackDeadLetters` | O rollback não aconteceu e o alarme de SLO segue em `ALARM` sem gerar evento novo. Leia a mensagem, corrija a causa e faça o rollback manual (`POST /api/v1/prompts/rollback`) |
| `ReviewWorkflowFailedAlarm` | Evento de log `{"action": "review_workflow_failed"}` na Lambda da API (metric filter → métrica `Prumo/ReviewWorkflowFailed`) | A classificação seguiu, mas a decisão sorteada não entrou (ou não saiu) da revisão humana: a estimativa perde rótulos |
| `ReviewExecutionsFailedAlarm` | `ExecutionsFailed` da máquina de revisão | Execução falhou depois dos 3 retries da `review_task` |
| `ReviewTaskErrors` | Erro na Lambda `review_task` | Token de revisão não foi gravado (cada retry gera token novo) |

Para inspecionar a DLQ:

```bash
QUEUE=$(aws sqs list-queues --queue-name-prefix PrumoStack-RollbackDeadLetters \
  --query "QueueUrls[0]" --output text)
aws sqs receive-message --queue-url "$QUEUE" --max-number-of-messages 10 \
  --message-attribute-names All --attribute-names All
```

## Como testar

```bash
OUT=$(aws cloudformation describe-stacks --stack-name PrumoStack --query "Stacks[0].Outputs")
API=$(echo "$OUT" | jq -r '.[] | select(.OutputKey=="ApiUrl").OutputValue' | sed 's:/*$::')
SECRET=$(echo "$OUT" | jq -r '.[] | select(.OutputKey=="AdminTokenSecretArn").OutputValue')
TOKEN=$(aws secretsmanager get-secret-value --secret-id "$SECRET" --query SecretString --output text)

curl -s "$API/health"
curl -s -X POST "$API/api/v1/decisions" -H 'content-type: application/json' \
  -H "X-Prumo-Token: $TOKEN" \
  -d '{"lead_text": "Sou médica, 38 anos. Movimento R$ 42.000 por mês, sem empréstimos."}'
curl -s "$API/api/v1/review/queue?limit=5" -H "X-Prumo-Token: $TOKEN"
curl -s -X POST "$API/api/v1/review/<id-da-decisao>" -H 'content-type: application/json' \
  -H "X-Prumo-Token: $TOKEN" -d '{"correct": true, "reviewer": "voce"}'
curl -s "$API/api/v1/quality"
curl -s -X POST "$API/api/v1/prompts/v2/deploy" -H "X-Prumo-Token: $TOKEN"
```

Rodar o estimador sem esperar a hora cheia:

```bash
FN=$(aws lambda list-functions \
  --query "Functions[?starts_with(FunctionName, 'PrumoStack-EstimatorFunction')].FunctionName" \
  --output text)
aws lambda invoke --function-name "$FN" /dev/stdout
```

Testar a fiação alarme → rollback (o rollback só acontece se as últimas leituras da versão no ar
estiverem de fato abaixo da meta; caso contrário a Lambda registra
`rollback_skipped_not_consecutive`, que é a trava de segurança funcionando). Para desligar o
rollback automático sem novo código, defina `PRUMO_AUTO_ROLLBACK=false` nas Lambdas `rollback` e
`estimator`:

```bash
ALARM=$(aws cloudwatch describe-alarms --alarm-name-prefix PrumoStack-MonitoringSloBreached \
  --query "MetricAlarms[0].AlarmName" --output text)
aws cloudwatch set-alarm-state --alarm-name "$ALARM" --state-value ALARM \
  --state-reason "teste manual da fiação"
```

Os logs de cada Lambda estão no log group indicado em
`aws lambda get-function-configuration --function-name <nome> --query LoggingConfig.LogGroup`;
as execuções da revisão humana em
`aws stepfunctions list-executions --state-machine-arn <ReviewStateMachineArn>`.

## Custos esperados

Valores de referência em `us-east-1`; confira as páginas de preço antes de decidir.

| Item | Parado (sem tráfego) | Observação |
|---|---|---|
| Métricas personalizadas (EMF) | ~US$ 2,40–5,40/mês | 4 a 9 métricas × 2 combinações de dimensão × US$ 0,30 |
| Alarmes (8) | US$ 0,80/mês | |
| Metric filter `ReviewWorkflowFailed` | ~US$ 0 | Sem valor padrão: só vira métrica cobrada nos meses com falha |
| Secrets Manager (1 segredo) | US$ 0,40/mês | Mais US$ 0,05 por 10 mil leituras (uma por cold start da API) |
| SQS (DLQ do rollback) | ~US$ 0 | Só recebe mensagem quando um rollback falha |
| AppConfig | < US$ 1/mês | cobra por configuração recebida a cada sessão nova |
| Lambda, Scheduler, DynamoDB, X-Ray, SNS, HTTP API | ~US$ 0 | dentro do nível gratuito ou centavos |
| **Total parado** | **~US$ 4,50–7,50/mês** | |
| Bedrock por decisão | ~US$ 0,0025 | o juiz (Haiku 4.5) domina; ~US$ 2,50 por mil decisões |
| Step Functions Standard | ~US$ 0,0001 por revisão | 3–4 transições por execução |

O throttling da API (10 req/s por padrão) é também um teto de gasto com Bedrock. Ligue o
`budgetEmail` mesmo em conta pessoal.

## Armadilhas

- **NAT Gateway**: não coloque as Lambdas numa VPC "por segurança". Sem NAT elas não alcançam
  Bedrock, AppConfig e Step Functions, e um NAT custa ~US$ 32/mês por AZ mais tráfego (endpoints
  de VPC custam ~US$ 7/mês cada por AZ). A stack não usa VPC de propósito.
- **OpenSearch Serverless**: criar uma Knowledge Base do Bedrock pelo "quick create" sobe uma
  coleção do OpenSearch Serverless que cobra OCUs mínimas 24 h por dia (centenas de dólares por
  mês, com ou sem uso). Nada no Prumo precisa disso.
- **Estimador rodando sem tráfego**: ele continua lendo de hora em hora e publicando métricas,
  que são a maior parte do custo parado. Para pausar, desabilite a schedule **e** as ações do
  alarme dead-man (senão ele dispara, como deveria); para parar de pagar, destrua a stack.
- **Dead-man logo após o primeiro deploy**: o alarme trata "sem dados" como falha, então pode
  disparar antes da primeira execução agendada. Invoque o estimador uma vez (comando acima).
- **Deploy inicial do AppConfig**: a versão inicial é implantada por um custom resource que chama
  `StartDeployment` só no Create; no Update ele não faz nada e mantém o mesmo id físico. Um
  `AWS::AppConfig::Deployment` seria recriado a cada mudança de propriedade e **reimplantaria a v1
  por cima do prompt em produção**, desfazendo deploys e rollbacks. Por isso os testes da infra
  travam os logical ids `PromptsInitialVersion…`, `PromptsAllAtOnce…` e
  `PromptsInitialDeployment…`, o documento inicial e a estratégia. Renomear/mover esses
  constructs cria um recurso novo, e um custom resource novo faz Create de novo (= reimplanta a
  v1). Depois do primeiro deploy, a versão ativa é estado de runtime: mude pela API
  (`/api/v1/prompts/{versão}/deploy`).
- **Timeout da API**: classificador e juiz rodam em sequência e o HTTP API corta em 30 s,
  contando o cold start. A stack fixa `PRUMO_BEDROCK_TIMEOUT_SECONDS=8` e
  `PRUMO_BEDROCK_MAX_ATTEMPTS=1` (sem retry dentro da requisição): pior caso 2 × 1 × (3 s de
  conexão + 8 s de leitura) = 22 s, com folga para init e DynamoDB/AppConfig. Duas tentativas
  dariam 44 s; um teste da infra recusa qualquer combinação que não caiba nos 29 s da Lambda.
  Throttling do Bedrock vira erro 503 imediato para o cliente, que pode repetir.
- **Throttling não é autenticação**: o limite de req/s segura custo; quem barra chamadas de
  terceiros nas rotas de decisão e revisão é o token.
- **Reserved concurrency**: não use nas Lambdas. Conta nova tem cota de 10 execuções simultâneas
  e a AWS recusa reservas que deixem menos que o mínimo livre, quebrando o deploy. O rollback
  não precisa: a troca de versão é compare-and-swap.
- **Pacote das Lambdas**: o build mira `aarch64-manylinux_2_34` (glibc do Amazon Linux 2023).
  Não use `pip install` comum no macOS: os wheels sairiam para a máquina errada.

## Como destruir

```bash
cd infra
npm ci
npx --no -- cdk destroy PrumoStack          # use -c dev=true se foi criada assim
```

Em modo normal (sem `dev`), ficam de propósito, para não perder dados por engano: a tabela do
DynamoDB (com proteção contra deleção), o segredo do token, a fila `RollbackDeadLetters` e os
log groups. Para apagá-los de
vez, depois de conferir que não precisa mais dos dados:

```bash
aws dynamodb update-table --table-name <DecisionsTableName> --no-deletion-protection-enabled
aws dynamodb delete-table --table-name <DecisionsTableName>
aws secretsmanager delete-secret --secret-id <AdminTokenSecretArn> --recovery-window-in-days 7
aws logs describe-log-groups --log-group-name-prefix PrumoStack --query "logGroups[].logGroupName"
```

Se o `destroy` parar no AppConfig por "configuração usada recentemente" (proteção de deleção do
AppConfig), espere o período de proteção (60 min por padrão) e rode de novo. Com `-c dev=true`
essa proteção é ignorada.
