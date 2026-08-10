# Estratégia V10 — Pré-Explosão e Reversão de Range

## Objetivo

Capturar movimentos direcionais potentes desde o rompimento de uma compressão e permitir uma nova operação no sentido contrário quando o primeiro movimento terminar e a estrutura inverter.

Não é uma estratégia de “adivinhar topo ou fundo”. A preparação arma o candidato; preço e fluxo em tempo real disparam a entrada.

## Problema que resolve

A estratégia `OI_EXPANSION_CONFIRMATION` atual começa a observar depois de um impulso e ainda espera uma nova janela de confirmação. Isso reduz entradas ruins, mas pode chegar tarde e perder boa parte da expansão.

O novo desenho observa três momentos distintos:

1. preparação antes do rompimento;
2. rompimento com confirmação em tempo real;
3. falha, lateralização ou reversão após o movimento.

## Estados do setup

```text
SCANNING
  -> COMPRESSION
  -> PRE_ARMED_LONG ou PRE_ARMED_SHORT
  -> TRIGGERED
  -> POSITION_OPEN
  -> FOLLOW_THROUGH | NO_FOLLOW_THROUGH | REVERSAL_READY
```

Cada transição deve ser gravada no ledger com os valores das métricas e o motivo da decisão.

## 1. Radar de compressão

Executado sobre todo o universo USDT Perpetual usando dados leves.

- Range/ATR de 1m e 5m comprimido em relação ao histórico recente.
- Distância pequena da máxima ou mínima estrutural.
- Liquidez e spread adequados.
- Volume começando a crescer, sem candle já excessivamente esticado.
- Símbolo classificado como candidato; ainda não existe ordem.

## 2. Pré-armado com derivativos

Somente candidatos da compressão recebem consultas mais pesadas.

- OI atual e inclinação do OI em 5m e 15m.
- Taker buy/sell e sua aceleração, não apenas o valor atual.
- LSR global e variação do LSR.
- Funding como bloqueio de extremo, não como direção isolada.
- Contexto de BTC e do mercado para evitar operar contra choque sistêmico.

Interpretação mínima de preço x OI:

| Preço | OI | Leitura inicial |
|---|---|---|
| sobe | sobe | construção compradora/continuação possível |
| cai | sobe | construção vendedora/continuação possível |
| sobe | cai | fechamento de shorts; evitar perseguir |
| cai | cai | liquidação de longs; evitar perseguir |

OI não define direção sozinho: todo contrato possui comprado e vendido. A direção vem da combinação entre estrutura de preço, agressão e resposta do mercado.

## 3. Gatilho de entrada em tempo real

Usar WebSocket de Futures para preço, negócios agregados e melhor bid/ask. O scanner de 60 segundos não pode ser o gatilho final.

LONG:

- fechamento/aceitação acima da máxima do range;
- delta agressor comprador confirma;
- volume acelera;
- OI permanece estável ou acelera;
- taker comprador não está em clímax;
- distância da fronteira ainda permite RR executável.

SHORT é simétrico abaixo da mínima do range.

## 4. Proteções contra entrada atrasada

- Limite de distância entre preço executável e fronteira rompida.
- Limite de slippage e divergência entre mercado real e Testnet.
- Bloqueio quando taker/volume já atingiram clímax sem avanço proporcional de preço.
- Cancelamento do candidato quando o preço volta para dentro do range.
- Um rompimento perdido não deve ser perseguido.

## 5. Gestão de posição para explosão

- Stop atrás da estrutura do range, normalmente limitado a 0,25%–0,50% conforme volatilidade.
- Se em 3–5 minutos não houver follow-through mínimo, realizar saída técnica antecipada.
- Parcial opcional em 1R.
- Após confirmação, proteger no breakeven ou atrás do último microfundo/microtopo.
- Runner acompanhado por estrutura de 1m, buscando aproximadamente 2,5R–4R quando o mercado permitir.
- TP não deve ser ampliado junto com um SL arbitrariamente maior.

## 6. Reversão e movimentos de ida e volta

Depois de uma expansão:

- detectar lateralização e registrar máximo/mínimo do novo range;
- encerrar posição sem follow-through antes de ficar presa;
- liberar `REVERSAL_READY` se a base/topo for rompida com OI e agressão na direção contrária;
- a reversão é um novo setup completo, nunca apenas uma ordem inversa automática.

Isso permite procurar um LONG no rompimento inicial e, mais tarde, um SHORT na perda confirmada da base — ou o inverso.

## 7. Arquitetura de dados

- WebSocket all-market/mark price para vigiar o universo.
- Streams direcionados de `aggTrade` e `bookTicker` para candidatos pré-armados.
- OI atual consultado com maior frequência apenas para candidatos.
- Históricos de OI, taker e LSR usados para inclinação e contexto.
- Reconexão, detecção de dados stale e relógio do servidor obrigatórios.

## 8. Painel e auditoria

O painel deve mostrar:

- quantidade em `COMPRESSION`, `PRE_ARMED`, `TRIGGERED` e `REVERSAL_READY`;
- fronteiras do range no gráfico;
- OI, LSR, taker e delta sincronizados com o preço;
- motivo exato de cada bloqueio;
- tempo até follow-through;
- MFE, MAE, resultado em R e custos reais;
- distinção entre sinal público e fill da Testnet/Real.

## 9. Validação antes do modo real

1. Replay histórico sem olhar candles futuros.
2. Teste walk-forward por período e símbolo.
3. Testnet com parâmetros congelados.
4. Separar resultados LONG, SHORT, rompimento e reversão.
5. Avaliar expectativa líquida, profit factor, drawdown, MFE/MAE e frequência.
6. Não promover para Real com amostra pequena ou expectativa dependente de poucos outliers.

## Decisão registrada em 28/07/2026

A estratégia atual permaneceu aproximadamente 4h20 em Testnet, armou 14 candidatos, invalidou 8, expirou 6 e não gerou entrada. Ela será preservada como controle. A próxima refatoração deve implementar este radar de pré-explosão/reversão, sem misturar os ledgers das duas estratégias.
