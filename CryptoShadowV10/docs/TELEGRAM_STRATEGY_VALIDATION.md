# Validacao das hipoteses Telegram - 03/08/2026

## Escopo

- 12 contratos perpetuos USDT com maior volume de cotacao no momento da coleta.
- 29 dias de candles, OI, global LSR e taker em periodos de 5 minutos.
- Aproximadamente 8.352 periodos por ativo.
- Entrada sempre na abertura do candle seguinte ao sinal.
- Custo pessimista: 0,10% de taxas por round trip mais 0,02% de slippage por lado.
- Se stop e alvo aparecem no mesmo candle, o stop e considerado primeiro.
- Primeiros 60% do tempo como desenvolvimento e ultimos 40% como validacao.

Os timestamps foram alinhados pela semantica da Binance: candle e taker usam inicio do
periodo; OI e global LSR usam fim do periodo. Portanto, OI/LSR do fechamento `T` sao
associados ao candle iniciado em `T-5m`, sem antecipar dados.

## Modelos traduzidos

### CONTINUATION_CORE

Consolidacao de 24 barras, dois fechamentos sustentando o rompimento e:

- Long: OI 15m positivo e slope de LSR 15m negativo.
- Short: OI 15m negativo e slope de LSR 15m positivo.

### CONTINUATION_FLOW

Mesmo nucleo, adicionando volume, numero de negocios e taker na direcao.

### FAILED_BREAK_REVERSAL

Excursao alem da fronteira, fechamento de volta ao range e taker confirmando o retorno.

## Resultado principal em 5 minutos, alvo 2R

| Modelo | Trades validacao | PF validacao | Media R validacao | Situacao |
|---|---:|---:|---:|---|
| CONTINUATION_CORE | 256 | 0,751 | -0,1769R | Reprovado |
| CONTINUATION_FLOW | 142 | 0,676 | -0,2304R | Reprovado |
| FAILED_BREAK_REVERSAL | 1.002 | 0,432 | -0,5633R | Reprovado |

Alvos de 1R, 1,5R, 2R e 3R permaneceram negativos tanto no desenvolvimento quanto na
validacao. Logo, o problema nao e resolvido por alongar TP.

## Resultado em 15 minutos

O nucleo de continuacao melhorou para PF 0,813 e -0,1178R medio na validacao, mas
continuou negativo. Adicionar fluxo reduziu o PF para 0,706. Dar mais espaco temporal e
estrutural, isoladamente, nao criou edge.

## Efeito dos custos

Sem taxa nem slippage, `CONTINUATION_CORE` ficou:

- Desenvolvimento: PF 0,921 e -0,0490R medio.
- Validacao: PF 1,317 e +0,1665R medio.

Com custos realistas, todas as semanas ficaram negativas. A mediana do risco ate o stop
foi 0,536% e o custo assumido foi 0,14% por round trip; isso consome parcela grande do
risco. O sinal bruto tambem e instavel: duas semanas foram positivas sem custos e duas
negativas.

## Estudo de quadrantes sem stop/TP

Foram separados todos os rompimentos sustentados pelos sinais de OI e LSR. O unico
quadrante com media liquida positiva em 30m, 60m e 180m foi:

`LONG / OI_UP / LSR_DOWN`

Em 180 minutos, 313 eventos tiveram media de +0,5143%, mas mediana de -0,0977%. A media
foi dominada por poucos movimentos extremos:

- Semana iniciada em 30/07: +2,9717% medio.
- Semanas anteriores: -0,3600%, -0,0070%, -0,4420% e -0,3974%.
- `1000RATSUSDT`: +3,2354% medio em 45 eventos, com mediana ainda negativa.
- A maioria dos demais ativos ficou negativa.

Todos os quadrantes short apresentaram media liquida negativa nos horizontes avaliados.
Isso reprova, por enquanto, o mapa short simplificado do grupo.

## Diagnostico

1. OI/LSR melhora alguns rompimentos long, mas nao produz vantagem distribuida por tempo
   e ativos.
2. A media positiva de longo prazo e cauda: poucos pumps pagam muitos falsos sinais.
3. Stops estreitos e giro alto tornam a estrategia inviavel depois de custos.
4. Volume/taker como filtros binarios nao resolveram o timing.
5. Falso rompimento simples ocorre demais e nao distingue sweep valido de ruido.
6. A engine nao deve inverter automaticamente trades nem executar as regras Telegram
   atuais.

## Proxima hipotese permitida

Pesquisar um modelo long-only de campanha, inicialmente apenas em observacao, que:

- detecte acumulacao multi-timeframe em vez de janela fixa;
- exija sustentacao ou reteste, nao apenas dois closes;
- modele OI/LSR por slope, persistencia e aceleracao;
- identifique regime e liquidez do ativo;
- tenha distancia estrutural suficiente para que custos representem pequena fracao do R;
- use saida de cauda/estrutura, pois o possivel retorno vem de poucos movimentos grandes.

Essa hipotese ainda precisa ser testada em nova janela temporal. Nao esta aprovada para
execucao Testnet ou Real.
