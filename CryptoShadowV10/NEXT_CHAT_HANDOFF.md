# Handoff — CryptoShadow V10

Atualizado em: 2026-08-10 16:10 (America/Sao_Paulo)

## Objetivo

Continuar a observação da correção staged dos fades sem perder o histórico da análise V9. Não resetar amostras, não alterar a estratégia e não operar a Binance real antes de existir evidência quantitativa suficiente.

## Código publicado

- Repositório: `hassegawa91/FarmCreatures`
- Projeto separado: `CryptoShadowV10/`
- Branch padrão: `main`
- Commit inicial publicado: `88b4ed17395342d67356b5fccf6e8601e121360f`
- O projeto do jogo foi preservado; o commit do V10 apenas adicionou a nova pasta.
- `.env`, credenciais, bancos, logs, caches, temporários e ambientes virtuais não estão no Git.

## Estado da revisão atual

- Diretório operacional local: `C:\v10`
- Revisão atual: `FADE_PROBE_ONLY_SHADOW_V11`
- Início desta revisão: `2026-08-10T17:35:00-03:00`
- Início da amostra: `2026-08-10T09:37:30-03:00`
- Modo: Testnet; Binance real zerada pelo usuário.
- `VOLATILITY_EXHAUSTION_FADE_SCALP_V1` está em `testnet_observation_only_setups`.
- A Shadow Real permanece observacional, usando mercado real sem enviar ordens reais.
- A suíte possui 169 testes e estava integralmente aprovada na publicação.

## Correção staged em teste

A correção não foi considerada validada pela amostra V9. Ela é um teste A/B paralelo para reduzir a exposição inicial sem eliminar os sinais da amostra:

- Fade LONG: probe de 25% da margem e add dos 75% restantes somente após `+0.20R`.
- Fade SHORT: probe de 10% da margem, sem add.
- Margem de referência: 50 USDT; alavancagem de referência: 10x.
- Os sinais continuam sendo registrados na Shadow/simulação para permitir comparação com a execução integral.
- O objetivo é medir se a confirmação paga o custo de entradas perdidas e se corta a cauda negativa. Não assumir melhora antes de comparar os mesmos sinais.

## Primeira leitura da amostra V10

Auditoria de `data/v10.sqlite` desde o início da revisão:

- 5 sinais aceitos e 5 replays públicos.
- 4 operações Testnet encerradas e 1 pendente.
- Todas as 4 encerradas eram `VOLATILITY_EXHAUSTION_CONTINUATION_SCALP_V1`, portanto ainda não validam a correção dos fades.
- PNL líquido Testnet: `+17.0484 USDT`.
- Win rate: `75%`; profit factor: `6.084`; média: `+0.746R`.
- Saídas: 1 `STOP`, 2 `RUNNER_STOP` e 1 `THESIS_EXIT`.
- A amostra é muito pequena e não autoriza conclusão de expectativa.

## Dados locais

Os dados brutos somam aproximadamente 5,98 GB e não foram enviados ao GitHub. Permanecem disponíveis nesta máquina:

- `data/v10.sqlite`
- `data/real_shadow.sqlite`
- `data/limited_shadow.sqlite`
- `data/simulations.sqlite`
- V9 arquivada em `data/archive/sample_reset_20260810_0911_v9`
- Auditoria atual em `tmp/current_sample_audit_20260810.json`

Um ChatGPT trabalhando fora desta máquina terá acesso ao código e a este handoff pelo GitHub, mas não aos bancos locais. Para análise remota, gerar extratos sanitizados e compactos; nunca publicar bancos completos, `.env` ou chaves.

O painel possui botões para gerar esses extratos sob demanda:

- `Baixar Testnet`: sinais, execuções, resultados, eventos e observações de features.
- `Baixar tudo`: reúne Testnet, Shadow Real, Shadow individual e staged em um único ZIP.
- `Baixar Shadow Real`: trades e eventos do mercado real simulado.
- `Baixar Shadow individual`: ledger da Shadow limitada.
- `Baixar correção staged`: trades e eventos de todas as simulações paralelas.

Cada botão baixa um ZIP com JSONL, manifesto de contagens e `config.sanitized.json`. Valores de chaves, segredos, tokens, senhas e credenciais são removidos automaticamente. Os ZIPs podem ser anexados diretamente a outro ChatGPT.

A análise quantitativa completa do snapshot de 17:35 está em `docs/ANALISE_RESULTADOS_V10_20260810.md`. Ela concluiu que a diferença Testnet × Shadow era principalmente composição de sinais: continuações positivas nos dois ambientes e fades negativos somente na Shadow. O add LONG após `+0,20R` foi desabilitado para novas simulações; probes LONG 25% e SHORT 10% continuam sem add.

O painel também possui `ZERAR TUDO`. Ele só é permitido em modo Testnet e exige digitar `ZERAR_TUDO_TESTNET_SHADOW` mais uma segunda confirmação. A rotina cria backups ZIP em `data/archive/panel_reset_*`, encerra posições e ordens Testnet e somente depois limpa Testnet, Shadow Real, Shadow individual e simulações. Se o fechamento da Testnet falhar, nenhum ledger é apagado. O botão não opera nem limpa a Binance real.

## Verificação operacional necessária

O painel foi reiniciado após a implantação dos exports e respondeu saudável em `http://127.0.0.1:8000/health`, modo Testnet e coleta ativa. O PID observado após o restart era 12476. Sempre preservar os ledgers ao reiniciar.

## Próxima análise

1. Confirmar que o serviço e a coleta voltaram a responder sem zerar bancos.
2. Medir por direção os fades baseline versus staged sobre exatamente os mesmos sinais.
3. Comparar N, PNL, expectativa em R, PF, MFE/MAE, custo evitado, ganhos sacrificados e exposição média.
4. Separar LONG e SHORT; a assimetria do probe é intencional.
5. Não promover a correção para execução real com amostra pequena. Buscar pelo menos dezenas de fades encerrados e estabilidade fora de um único regime.
6. Se o saldo Testnet limitar a observação, manter a Shadow/simulação como fonte principal em vez de relaxar risco para gerar trades.

## Prompt para o próximo ChatGPT

`Abra o projeto CryptoShadowV10 no repositório hassegawa91/FarmCreatures, leia NEXT_CHAT_HANDOFF.md e docs/ANALISE_RESULTADOS_V10_20260810.md. Continue o acompanhamento da revisão FADE_PROBE_ONLY_SHADOW_V11. Não resete os bancos e não trate os primeiros resultados como validação. Se estiver nesta máquina, use os bancos locais para comparar baseline versus probe-only nos mesmos sinais.`
