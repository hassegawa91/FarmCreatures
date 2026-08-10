# Handoff curto — análise dos losses V9

Capturado em: 2026-08-10 08:43 (America/Sao_Paulo)

## Objetivo do novo chat

Diagnosticar quantitativamente por que a amostra V9 ficou negativa. Não zerar ledgers, não encerrar posições e não alterar a engine antes de concluir a análise causal de Testnet e Shadow Real.

## Estado operacional

- Diretório: `C:\v10`
- Revisão: `FRESH_CONTINUATION_REAL_FIRST_V9`
- Início da amostra: `2026-08-09T21:19:09-03:00`
- Engine rodando, sem erros.
- Testnet: 58 fechados, 4 abertos, PNL fechado `-83.79598337 USDT`, win rate `43.10%`, PF `0.5541`, expectativa `-0.1269R`.
- Shadow Real: 67 fechados, 5 abertos, PNL fechado `-36.38355984 USDT`, aberto `-9.26393772 USDT`, PF `0.7720`, win rate `53.73%`.
- Shadow limitada: 32 fechados, 2 abertos, PNL fechado `-28.20679130 USDT`, aberto `-5.04755270 USDT`, PF `0.6352`.
- Posições Testnet abertas no snapshot: EPIC SHORT, TRUMP LONG, LA SHORT e BICO LONG.

## Hipótese inicial a testar — não assumir como conclusão

A taxa de acerto não é o problema isolado: os ganhos típicos do runner são pequenos, enquanto vários `THESIS_EXIT`/`STOP` são grandes. Verificar assimetria de payoff, entradas sem MFE, slippage Testnet, giveback do runner, direção/regime, setup e duplicidade por símbolo.

Exemplos recentes de losses Testnet: ACE `-14.58`, NIL `-10.01`, BEAT `-9.39`, SKYAI `-6.40`, 4USDT `-6.61` e ARC `-6.13` USDT. Houve runners relevantes, como BICO `+14.20` e HOME `+7.41`, mas não compensaram a cauda negativa.

## Fontes obrigatórias

- `data/v10.sqlite`: execuções e resultados Testnet.
- `data/real_shadow.sqlite`: Shadow com preços/livro reais.
- `data/limited_shadow.sqlite`: variante com limite de posições.
- `data/simulations.sqlite`: simulações paralelas.
- `config.json`: parâmetros efetivamente usados.
- `engine/campaign.py`, `engine/volatility_scalp.py`, `engine/dump_reclaim.py`, `engine/real_shadow.py`, `engine/execution.py`.
- Amostra anterior arquivada: `data/archive/sample_reset_20260809_211909_v8`.

## Análise solicitada

1. Gerar tabela por setup e ambiente: N, wins/losses, PNL, PF, média/mediana, MFE, MAE, duração, taxas, motivo de saída e payoff médio win/loss.
2. Separar `VOLATILITY_EXHAUSTION_FADE_SCALP_V1`, `VOLATILITY_EXHAUSTION_CONTINUATION_SCALP_V1`, dumps e borders.
3. Comparar os mesmos sinais Testnet × Shadow Real e explicar divergências de fill, stop, slippage e resultado.
4. Identificar características comuns dos maiores losses usando evidências de entrada: impulso 5m/15m, OI 5m/15m, taker, LSR, ADX, ATR, volume, spread, idade do arm e direção do BTC.
5. Medir quantos losses tiveram MFE zero/quase zero, quantos chegaram ao runner, quanto devolveram e quantos teriam sido evitados por lógica causal — sem simplesmente aumentar filtros.
6. Fazer contrafactuais nos candles públicos: saída atual versus stop maior/menor, entrada atrasada, runner alternativo e ausência de `THESIS_EXIT`.
7. Entregar primeiro o diagnóstico com evidências. Só depois propor a menor correção capaz de melhorar expectativa sem reduzir drasticamente a quantidade de entradas.

## Prompt para iniciar o novo chat

`Leia C:\v10\NEXT_CHAT_HANDOFF.md e faça a análise completa da amostra V9. Priorize a Shadow Real, compare com a Testnet e não altere nem zere nada antes de apresentar o diagnóstico quantitativo dos losses.`
