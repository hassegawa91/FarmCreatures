# Base de referencias de mercado — Telegram

Esta base registra ideias observadas em grupos do Telegram para posterior validacao quantitativa. Mensagens, sinais e resultados publicados nao sao verdade por si mesmos e nao devem acionar ordens diretamente.

## Regras da pesquisa

- O Telegram serve como fonte de hipoteses, nunca como gatilho automatico.
- Cada ideia precisa virar uma regra objetiva e ser validada em replay/walk-forward.
- Resultados divulgados pelos canais podem conter vies de selecao.
- Propaganda, afiliados, alavancagem sugerida e alegacoes de lucro nao entram na engine.
- A engine so muda depois de comparacao com uma amostra congelada.

## Amostra 2026-08-02

### Encryptos

Assuntos tecnicos encontrados na amostra recente e na busca historica:

- Alertas de variacao de OI em 5 minutos e 1 hora, em valor absoluto e percentual.
- Uso da divergencia entre preco e OI como contexto, em vez de interpretar OI isoladamente.
- Observacao de que o OI pode estar concentrado ou divergente entre corretoras.
- Uso do nivel, variacao e persistencia do LSR; extremos isolados nao determinam imediatamente a direcao.
- Exemplo de leitura de crowding: LSR do BTC proximo de 2 junto de discussao sobre longs servindo de liquidez.
- Uso de BTC.D e USDT.D para determinar se altcoins estao em ambiente favoravel ou defensivo.
- Atencao a moedas ganhando forca exponencial, seguida de pullback, rompimento repetido ou armadilha.
- Alertas como `OI 1h +4,55%`, `OI 5m +1,73%` e `OI 5m -4,70%` aparecem como radar, nao como direcao pronta.
- Exemplos de LSR extremos encontrados: 0,23, 0,49, 2,17, 3,96 e 4,50. A conversa mostra que um extremo pode persistir por dias/semanas.

Leitura para a V10:

1. OI deve ser classificado junto do preco:
   - preco sobe + OI sobe: construcao/continuacao possivel;
   - preco cai + OI sobe: construcao vendedora/continuacao possivel;
   - preco sobe + OI cai: fechamento ou liquidacao de shorts;
   - preco cai + OI cai: fechamento ou liquidacao de longs.
2. LSR precisa de nivel, inclinacao e duracao do extremo. Contrariar o crowding somente pelo valor atual e prematuro.
3. Uma divergencia deve primeiro armar o ativo. A entrada exige resposta do preco, fluxo e estrutura.
4. O regime de BTC.D/USDT.D pode reduzir ou aumentar o risco das altcoins, mas nao definir a entrada sozinho.

### Alertas Cripto Futuros

O proprio canal informa que compara o preco dos futuros com pivôs dos ultimos 30 minutos e alerta quando a variacao chega aproximadamente a +/-5% (BTC, +/-2%). Tambem declara que os alertas nao sao sinais de compra ou venda.

Conclusao:

- E um detector de movimento ja desenvolvido, util para identificar regime, outliers e candidatos a continuacao/reversao.
- E tarde demais para ser usado como entrada inicial de explosao.
- Perseguir a primeira notificacao de +5% ou -5% tende a aumentar entrada atrasada e risco de correcao.
- Pode ser usado futuramente como rotulo para medir se o radar interno da V10 detectou o ativo antes do alerta publico.

### AMAN VIP CRYPTO SIGNAL (amostra de sinais)

Padroes observados:

- Entradas por zona, nao por um unico preco.
- Stops estruturais significativamente mais largos que os stops curtos usados em varias versoes anteriores da V10.
- Multiplos alvos e realizacao parcial.
- Depois de TP1/TP2, instrucao para realizar parte e mover o stop para a entrada.
- Exemplo de gatilho mais objetivo para BTC: fechamento confirmado de 5m acima do nivel e reteste bem-sucedido para LONG; fechamento abaixo e rejeicao do nivel para SHORT.
- Sinais recentes de ADA, AAVE, ETHFI, ETH e ONDO usam aproximadamente 10x, mas a alavancagem divulgada nao sera copiada.

Faixas de risco observadas nos exemplos recentes:

- ADA: zona ampla; dependendo do fill, distancia ao stop aproximada de 3% a 7,5%.
- AAVE: aproximadamente 3,6% a 5,4%.
- ETHFI: aproximadamente 4,0% a 5,4%.
- ETH: aproximadamente 1,9% a 3,0%.

Conclusao:

- O ponto relevante nao e copiar sinais nem usar 10x.
- A referencia util e esperar confirmacao/reteste, aceitar uma zona de entrada e dimensionar a quantidade pelo stop estrutural.
- Stop mais largo exige quantidade menor para manter o mesmo risco em USDT.
- Parciais precisam ser avaliadas liquidas de taxas; muitos alvos pequenos podem aumentar custo e reduzir expectativa.

### Crypto Sharks (amostra visivel)

Foi observado um sinal de BTC com:

- zona de entrada em vez de preco unico;
- risco declarado separadamente;
- varios alvos;
- percentual do deposito e alavancagem divulgados.

Conclusao:

- Reforca o padrao de zonas e saidas fracionadas.
- Percentual de deposito e alavancagem nao equivalem a risco real; a V10 continuara dimensionando pela distancia ate o stop e pelo risco maximo da conta.

## Hipoteses candidatas para teste

### H1 — OI relativo e aceleracao

Armar quando a variacao de OI do ativo for anormal em relacao ao proprio historico, usando percentil/z-score e aceleracao em 5m/15m. Nao entrar apenas por ultrapassar um limite fixo.

### H2 — Crowding persistente, nao instantaneo

Usar LSR extremo como contexto. Entrada contraria somente depois de falha estrutural, absorcao ou reclaim/rejeicao confirmada. Medir nivel, inclinacao e tempo no extremo.

### H3 — Impulso, pullback e reclaim

Ao detectar aceleracao preco + OI + fluxo, armar o ativo. Entrar na retomada depois de pullback controlado, evitando tanto perseguir o quarto candle quanto antecipar uma reversao sem confirmacao.

### H4 — Stop estrutural com risco constante

Permitir stop mais largo quando a estrutura exigir, reduzindo proporcionalmente a quantidade. Comparar com stop curto usando MAE, MFE, taxa de stop precoce e expectativa liquida.

### H5 — Regime de dominancia

Usar BTC.D/USDT.D apenas como multiplicador de risco ou bloqueio de altcoins quando houver ruptura clara do regime. Nao usar como sinal direcional isolado.

### H6 — Alerta publico como benchmark de antecedencia

Medir quantos minutos antes/depois a V10 detecta ativos que posteriormente aparecem em alertas de +/-5%. Objetivo: detectar preparacao antes do movimento, nao copiar o alerta atrasado.

## Campos que a engine deve registrar para validar as hipoteses

- preco e retorno em 1m, 3m, 5m, 15m e 1h;
- OI em valor e percentual, inclinacao, aceleracao e percentil do ativo;
- matriz preco x OI;
- LSR atual, variacao, inclinacao e duracao no extremo;
- taker ratio, delta agressor, volume relativo e spread;
- BTC.D/USDT.D ou proxy de regime, quando disponivel;
- instante do arm, pullback, reclaim, fill, parcial e saida;
- MAE, MFE, resultado em R, taxas e motivo da saida;
- classificacao `antes_do_alerta`, `junto_do_alerta` ou `depois_do_alerta`.

## Proxima etapa recomendada

Continuar a coleta por varios dias, cruzar cada hipotese com candles e derivativos reais e somente entao escolher uma alteracao isolada para teste A/B. Nenhuma mensagem desta base autoriza entrada automatica ou mudanca imediata na estrategia.

## Aplicacao controlada na V10 - 2026-08-02

Primeira hipotese colocada em teste: H3 (impulso, pullback e reclaim).

- A confirmacao direta continua exigindo uma nova janela de 5m e uma nova amostra de OI.
- Um reteste real pode confirmar antes, mas somente em novo candle fechado de 1m, com reclaim, OI ainda sustentado, fluxo/taker coerente, LSR sem deterioracao, volume, spread e funding validos.
- A amostra de OI mantida durante o reteste so e aceita enquanto tiver no maximo 6 minutos; isso respeita a granularidade de 5m da Binance sem fingir que o dado atualiza a cada minuto.
- A entrada imediata `OI_MOMENTUM_EARLY` passa a `OBSERVE`: continua gerando evidencia comparativa, mas nao abre campanha perseguindo candle esticado.
- Cada sinal de expansao agora registra `entry_model` (`DIRECT_CONFIRMATION` ou `RETEST_RECLAIM`), `price_oi_regime` e contexto de LSR para permitir comparacao separada no ledger.
- Nenhum limite de OI, volume, taker, spread, funding ou risco foi afrouxado.

## Auditoria externa e semantica das metricas - 2026-08-02

Fontes primarias consultadas:

- Binance USD-M Futures Market Data: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
- Silantyev, *Order flow analysis of cryptocurrency markets* (2019): https://doi.org/10.1007/s42521-019-00007-w
- He, Manela, Ross e von Wachter, *Fundamentals of Perpetual Futures* (2022): https://arxiv.org/abs/2212.06888

Conclusoes aplicadas:

- `globalLongShortAccountRatio` e uma razao da quantidade de contas long/short de todos os traders; nao mede tamanho das posicoes.
- A Binance possui metricas distintas para contas e posicoes dos top traders. Elas nao devem ser chamadas genericamente de LSR nem substituidas umas pelas outras.
- A interface passou a identificar a serie atual explicitamente como `LSR contas globais`.
- O taker buy/sell volume descreve agressao executada no intervalo. A literatura encontrada sustenta forte relacao contemporanea com preco, mas isso nao prova previsao fora da amostra; portanto taker continua como confirmacao, nao sinal isolado.
- Funding e mecanismo de alinhamento do perpetual ao subjacente e contexto de custo/crowding; nao sera usado sozinho para direcao de curtissimo prazo.
- Os endpoints historicos de OI, LSR e taker da Binance disponibilizam somente os ultimos 30 dias. A V10 precisa construir sua propria base continuamente.

Infraestrutura criada:

- Nova tabela SQLite `feature_observations` guarda snapshot completo e diagnostico de todas as estrategias para cada candidato pesado.
- Cada observacao recebe automaticamente o retorno futuro observado em 5m, 15m, 30m e 60m, mesmo quando nenhuma ordem e aberta.
- O status da engine expoe `research_sample` com contagem total e quantidade ja rotulada por horizonte.
- Essa base permitira comparar gates aprovados/reprovados, direcao proposta e retorno posterior antes de promover novas regras para execucao.
