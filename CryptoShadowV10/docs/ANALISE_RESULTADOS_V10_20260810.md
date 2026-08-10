# Análise comparativa V10 — Testnet, Shadow Real e Shadow limitada

Snapshot: 2026-08-10 17:35 (America/Sao_Paulo)
Amostra iniciada em: 2026-08-10 09:37:30-03:00

## Conclusão executiva

A diferença entre os três resultados não é causada principalmente por fills artificiais da Testnet. Ela é causada por **composição diferente de sinais**:

- A Testnet executou somente `VOLATILITY_EXHAUSTION_CONTINUATION_SCALP_V1`.
- A Shadow Real executou as mesmas continuações e também os fades, mantidos como observação.
- A Shadow limitada aceitou apenas os primeiros sinais disponíveis enquanto havia no máximo duas vagas; portanto é uma seleção temporal pequena, não uma estratégia independente validada.

Nos cinco sinais de continuação já encerrados em ambos os ambientes, a Testnet fez `+16,1135 USDT` e a Shadow Real `+14,9034 USDT`. Os sinais tiveram o mesmo lado vencedor/perdedor nos dois ambientes. A diferença de `+1,2101 USDT` a favor da Testnet é pequena perto do resultado e não explica o negativo da Shadow.

O responsável pelo negativo da Shadow foi o fade: 27 fechados, `-29,8754 USDT`, PF `0,561` e expectativa `-0,1094R`. A continuação na própria Shadow foi positiva: 5 fechados, `+14,9034 USDT`, PF `5,062` e `+0,5502R` médio.

## Quadro geral

| Ambiente | Fechados | Abertos | PNL fechado | WR | PF | R médio | Payoff médio win/loss |
|---|---:|---:|---:|---:|---:|---:|---:|
| Testnet | 5 | 1 | +16,1135 USDT | 60,0% | 4,757 | +0,5636R | 3,172 |
| Shadow Real | 32 | 7 | -14,9720 USDT | 56,25% | 0,791 | -0,0063R | 0,615 |
| Shadow limitada | 6 | 2 | +4,8757 USDT | 83,33% | 1,796 | +0,0902R | 0,359 |

O win rate da Shadow parece razoável, mas esconde o problema: ganho médio de `+3,1516 USDT` contra perda média de `-5,1214 USDT`. O sistema acerta mais da metade e ainda perde porque a cauda negativa é maior.

## Testnet versus Shadow nos mesmos sinais

| Símbolo | Lado | Testnet | Shadow | Leitura |
|---|---|---:|---:|---|
| AKE | SHORT | +1,2455 | +1,1412 | Resultado equivalente |
| ON | LONG | +12,8513 | +11,5905 | Mesmo runner; Testnet um pouco melhor |
| EUL | SHORT | -3,3536 | -2,1689 | Testnet pior; spread de execução 0,571% contra 0,016% na Shadow |
| CTSI | SHORT | +6,3052 | +5,8407 | Resultado equivalente |
| JOE | LONG | -0,9349 | -1,5001 | Mesma falha de tese; Shadow um pouco pior |
| COAI | LONG | aberta | aberta | Ainda sem resultado final |

Não há evidência nesta amostra de que a Testnet esteja sistematicamente fabricando winners. Há distorções pontuais de spread e drift, mas os sinais casados contam a mesma história. A ressalva é o N muito pequeno e a concentração do lucro em ON e CTSI.

## Causa dos losses

Todos os 14 losses encerrados da Shadow saíram por `THESIS_EXIT`:

- 7 por `price_failed_without_followthrough`;
- 6 por `reversal_failed_to_launch`;
- 1 por `progress_failed_after_giveback`.

Os 18 winners encerraram por `RUNNER_STOP` e somaram `+56,7282 USDT`. Os 14 thesis exits somaram `-71,7003 USDT`. Portanto, o problema é a relação entre o custo de uma tese que não lança e o lucro que o runner consegue preservar.

Nos fades especificamente:

- 15 winners somaram `+38,156 USDT`, com MFE médio `0,583R`;
- 12 losses somaram `-68,031 USDT`, com MFE médio de apenas `0,056R`;
- 10 dos 12 losses tiveram MFE menor ou igual a `0,10R` e responderam por `-56,787 USDT`;
- 7 dos 12 losses fecharam em até cinco minutos.

Isso caracteriza **entrada sem desenvolvimento**, não giveback de trades que chegaram a funcionar. A correção deve reduzir exposição inicial ou exigir confirmação econômica antes de aumentar tamanho — não apertar o runner dos vencedores.

### Direção

| Fade | N | PNL | WR | PF | R médio |
|---|---:|---:|---:|---:|---:|
| LONG | 11 | -10,056 USDT | 54,5% | 0,643 | -0,091R |
| SHORT | 16 | -19,819 USDT | 56,2% | 0,503 | -0,122R |

SHORT está pior, mas LONG também não possui expectativa positiva. Bloquear apenas SHORT melhora o dano, porém não resolve a tese do fade.

### Regime e filtros

Os losses de fade apresentaram ADX médio `33,72`, contra `25,87` nos winners, sugerindo reversão tentada em tendência ainda forte. Entretanto, filtros simples não generalizaram:

- Na Shadow atual, `ADX <= 30` teria mantido 59% das entradas e transformado o recorte em `+5,18 USDT`.
- Nas 117 simulações baseline, o mesmo filtro manteve somente 46% e continuou negativo: `-9,16%`, PF `0,733`.
- `ADX <= 35` mais no máximo três candles direcionais ficou positivo na Shadow pequena, mas negativo na simulação ampla.

Por isso, nenhum filtro de ADX/ATR foi promovido. Fazê-lo agora reduziria muito as entradas e seria ajuste por overfitting.

## Shadow limitada

A Shadow limitada tem apenas seis fechados: cinco winners por runner (`+11,0023 USDT`) e um thesis exit (`-6,1267 USDT`). O resultado positivo vem da seleção por disponibilidade de duas vagas e do baixo N. Ela não prova que limitar para duas posições melhora a expectativa; apenas mostra que reduziu a exposição e, nesta ordem temporal específica, evitou vários fades ruins e também vários bons.

## Correção staged

| Variante simulada | Fechados | PF | R médio | Resultado líquido percentual acumulado |
|---|---:|---:|---:|---:|
| Fade baseline | 117 | 0,723 | -0,0879R | -20,7718% |
| Probe staged | 33 | 0,865 | -0,0477R | -3,2783% |
| Add LONG após +0,20R | 8 | 0,063 | -0,0922R | -1,9947% |

O probe reduziu o dano por exposição, mas ainda não criou edge positivo. O add LONG foi claramente destrutivo no recorte: uma entrada tardia recebeu menos upside até o alvo original e continuou exposta ao stop original.

## Decisões

Aplicadas, por serem de baixo impacto:

1. Fades continuam bloqueados na Testnet e observados integralmente na Shadow; a continuação positiva não foi restringida.
2. O add LONG staged foi desabilitado para novas simulações. A revisão passou a `FADE_PROBE_ONLY_SHADOW_V11` em 2026-08-10 17:35-03:00.
3. O probe permanece em observação: 25% no LONG e 10% no SHORT, sem aumento posterior.
4. Foi criado `Baixar tudo`, reunindo os quatro pacotes sanitizados em um ZIP.

Não aplicadas:

- filtro rígido de ADX/ATR, por reduzir de 40% a 65% das entradas e não ficar positivo na amostra ampla;
- stop/thesis exit mais apertado, pois alguns winners tiveram MAE entre `-0,42R` e `-0,79R` antes de recuperar; sem replay temporal, a alteração pode cortar winners;
- promoção do fade para Testnet/real, pois baseline e probe ainda têm expectativa negativa;
- redução global das continuações, pois Testnet e Shadow confirmaram o mesmo edge nos sinais casados.

## Critério para próxima decisão

Continuar medindo sem resetar os ledgers. A próxima promoção deve exigir:

- pelo menos dezenas de probes encerrados por direção;
- PF acima de 1 e R médio positivo fora de um único regime;
- comparação nos mesmos sinais contra o baseline;
- nenhum add enquanto a geometria tardia não mostrar edge próprio;
- análise separada antes/depois de `FADE_PROBE_ONLY_SHADOW_V11` pelo timestamp de revisão.
