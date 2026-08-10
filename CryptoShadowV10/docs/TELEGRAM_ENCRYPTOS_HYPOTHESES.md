# Hipoteses extraidas do grupo Encryptos

Status: pesquisa em andamento. Estas regras ainda nao estao autorizadas como edge e
nao devem ser copiadas diretamente para execucao. Cada item precisa de backtest com
custos, slippage e validacao fora da amostra.

## Evidencia textual recorrente

O nucleo do setup descrito no grupo nao e "OI + LSR" isoladamente. A sequencia
recorrente e:

1. Encontrar uma acumulacao/caixote ou estrutura comprimida.
2. Tracar a linha de tendencia e observar mais de um timeframe.
3. Esperar o rompimento estrutural definir a direcao.
4. Confirmar posicionamento por mudanca de OI e LSR.
5. Confirmar atividade por aceleracao de negocios e volume.
6. Entrar no rompimento/reteste, invalidando atras da estrutura.

Referencias internas do lote inicial:

- `message500548` (29/11/2022 19:50): resumo explicito da acumulacao, trendline,
  OI, LSR, negocios por minuto e volume.
- `message503146` (01/12/2022 09:36): exemplo de triangulo 15m, contexto 1h,
  entrada de OI, queda do LSR e aceleracao de negocios.
- `message503651` (01/12/2022 15:22): o setup e descrito como saida de acumulacao.
- `message503756` (01/12/2022 16:26): OI subindo + LSR descendo + rompimento de LT.
- `message572606` (08/01/2023 14:02): short somente depois de perder pelo menos
  uma linha de tendencia no 5m.
- `message500574` (29/11/2022 20:26): entrada no rompimento e stop na saida da
  estrutura.
- `message556296` (29/12/2022 00:11): manutencao da posicao enquanto a linha de
  tendencia favoravel nao for perdida.

## Mapa direcional alegado pelo grupo

### Long

- Preco rompe a estrutura para cima.
- OI cresce: novos contratos entram.
- LSR cai: a maioria aumenta exposicao short, formando combustivel potencial.
- Negocios por minuto, volume e/ou agressao compradora aceleram.

O grupo chama a divergencia OI subindo / LSR descendo de "boca da jacaroa". A
abertura e tratada como combustivel; seu fechamento aparece como alerta de saida ou
perda de continuacao.

### Short

- Preco perde a estrutura para baixo.
- OI cai e LSR sobe no resumo mais repetido do setup.
- Negocios por minuto, volume e/ou agressao vendedora aceleram.

OI caindo nao prova abertura de shorts; pode representar fechamento ou liquidacao de
longs. Portanto, essa perna precisa obrigatoriamente de confirmacao direcional por
preco e fluxo. Sera testada contra alternativas com OI crescente, OI neutro e
decomposicao de OI em janelas diferentes.

## O que o grupo nao prova

- Prints de gain nao estimam taxa de acerto nem expectativa.
- Sinais publicados podem sofrer selecao, atraso e viés de sobrevivencia.
- "Smart money contra a maioria" e uma narrativa; precisa ser traduzida em series
  observaveis e testada.
- LSR agregado, LSR de contas, LSR de posicoes e top-trader LSR nao sao equivalentes.
- A correlacao entre OI/LSR e um movimento visto depois nao demonstra capacidade
  preditiva no instante da entrada.

## Material SMC do lote

O `SMC E-BOOK.pdf` foi extraido e suas 36 paginas foram revisadas visualmente. Os
elementos aproveitaveis como hipoteses objetivas sao:

- Estrutura por HH/HL/LH/LL, BOS e CHoCH.
- Liquidez em swing points, equal highs/lows, trendlines e extremos de ranges.
- Sweep/tomada de liquidez antes da reversao, sem assumir que todo toque reverte.
- Zona de interesse no timeframe maior e confirmacao no timeframe menor.
- Desequilibrio/FVG, supply/demand e order block como contexto, nao como ordem
  automatica isolada.
- Exemplo top-down: contexto 4h, zona 15m e confirmacao 5m.

Esses conceitos sao parcialmente subjetivos no documento. A implementacao devera
substituir desenhos manuais por definicoes reproduziveis de pivots, sweep, fechamento,
reclaim, deslocamento e invalidacao.

## Livro de analise tecnica do lote

As secoes pertinentes do livro tecnico foram localizadas no texto e conferidas
visualmente. Elas reforcam regras que podem ser objetivadas:

- Uma fronteira rompida por poucos ticks nao basta. A validade depende da volatilidade
  do ativo e de o preco sustentar o rompimento por mais de um periodo.
- Rompimento que volta rapidamente ao range e perde a microestrutura anterior e um
  `whipsaw`; essa falha pode produzir movimento forte na direcao oposta.
- O ponto de invalidacao deve ser definido antes da entrada, atras do suporte/resistencia
  estrutural que sustenta a premissa, e nao por percentual fixo arbitrario.
- Expansao de volume no rompimento de alta aumenta a confianca. Contracao de volume
  durante a fuga de alta e evidencia negativa, nao apenas ausencia de confirmacao.
- No rompimento de baixa, volume crescente reforca pressao vendedora. A contracao de
  volume e menos conclusiva porque pode acompanhar naturalmente a queda.
- Consolidacoes mais longas e profundas tendem a preceder movimentos maiores. O tamanho
  da estrutura e, portanto, uma variavel de potencial e de horizonte do trade.
- Preco e volume devem ser tratados como tendencias/janelas, nao por uma barra isolada;
  aberracoes pontuais sao normais.

Paginas visualmente revisadas: 112, 121, 132, 134-136, 142-148, 178, 180, 188,
221, 551 e 554 do PDF de analise tecnica.

## Hipoteses concorrentes encontradas nos audios

As primeiras transcricoes revelaram duas teses diferentes dentro do proprio material:

1. `CONTINUATION_BREAKOUT`: acumulacao, rompimento sustentado e confirmacao por
   OI/LSR/atividade.
2. `FAILED_BREAK_REVERSAL`: rompimento esticado ou sem sustentacao, retorno ao range e
   confirmacao de reversao antes de operar o lado contrario.

Essas teses nao podem compartilhar o mesmo gatilho. Serao implementadas e avaliadas como
maquinas de estado separadas. Um rompimento ainda nao confirmado nao autoriza nem a
continuacao nem a reversao; a decisao depende de sustentacao ou reclaim da estrutura.

Outros audios defendem esperar padrao de reversao apos vela muito esticada, evitar entrar
no climax e reconhecer que correcao faz parte do movimento. Essas observacoes sao
coerentes com medir extensao por ATR, persistencia do deslocamento e reteste, mas ainda
nao definem edge quantitativo.

Os PDFs `Bot Encryptos MacOS` e `O TRADER E A FALTA DE PACIENCIA` tambem foram revisados
integralmente. O primeiro e apenas tutorial de instalacao; o segundo trata de disciplina
e controle emocional. Nenhum dos dois fornece regra de entrada mensuravel.

## Triagem nos snapshots recentes da V10

Foram avaliadas 16.071 observacoes rotuladas, em 105 simbolos, com cooldown de 15
minutos por ativo. O custo preliminar usado foi 0,10% por round trip. Esta janela cobre
aproximadamente 27 horas e nao e suficiente para aprovar estrategia.

- Breakout long isolado: expectativa liquida media de -0,16% em 15m.
- Breakout long + OI positivo + slope de LSR negativo: -0,05% em 15m, +0,12% em
  30m e +0,47% em 60m.
- Adicionando volume e taker comprador: -0,07% em 15m, +0,22% em 30m e +0,66%
  em 60m; 214 eventos.
- Breakout short + OI negativo + slope de LSR positivo: -0,05% em 15m, +0,02%
  em 30m e +0,03% em 60m; 267 eventos.
- A combinacao bidirecional alegada pelo grupo ficou negativa em 5m/15m, levemente
  positiva em 30m e +0,22% de media liquida em 60m.

Leitura provisoria: OI/LSR parece melhorar a selecao em relacao ao breakout puro, mas
o efeito aparece tarde e depende de poucos movimentos grandes. Isso nao justifica
execucao ainda; indica que entrada, stop e horizonte precisam ser testados como uma
campanha estrutural, nao como scalp com alvo/stop curto.

Uma coleta independente posterior ampliou o teste para 29 dias e 12 ativos. As traducoes
diretas de continuacao, continuacao com fluxo e falso rompimento foram negativas com
custos no desenvolvimento e na validacao. O unico quadrante promissor em estudo de
eventos (`LONG/OI_UP/LSR_DOWN`) foi dominado pela ultima semana e por um unico ativo,
com mediana negativa. Resultados completos: `docs/TELEGRAM_STRATEGY_VALIDATION.md`.

## Divergencias encontradas na engine atual

1. A engine usa principalmente niveis absolutos de LSR; o setup enfatiza a inclinacao
   e a mudanca do LSR durante a acumulacao e o rompimento.
2. Varias estrategias exigem OI positivo tanto para long quanto para short. Isso nao
   representa a regra short descrita no grupo.
3. Direcao e frequentemente inferida pelo impulso de preco ja ocorrido. Isso favorece
   entrada atrasada e compra/venda de climax.
4. `prior_high/prior_low` de janela fixa nao representa necessariamente a trendline
   da acumulacao nem a estrutura de 5m/15m/1h.
5. Probes de antecipacao entram antes da confirmacao estrutural. A amostra recente ja
   mostrou que energia/compressao nao define direcao.
6. Ha estrategias de rompimento, antecipacao, momentum e reversao executando ao mesmo
   tempo, com premissas conflitantes.

## Hipotese quantitativa a ser testada

### Estado ARMADO

- Compressao objetiva por largura/ATR e contracao de volume.
- Duas fronteiras estruturais calculadas, com toques minimos e qualidade registrada.
- Contexto 5m, 15m e 1h; BTC/mercado como variavel, nao veto arbitrario.
- Series sincronizadas de preco, OI, tipo de LSR, volume, numero de negocios e taker.

### Gatilho LONG

- Fechamento acima da fronteira ou rompimento seguido de reteste valido.
- Delta/slope de OI positivo.
- Delta/slope de LSR negativo.
- Aceleracao de negocios e volume; taker comprador como confirmacao, nao como direcao
  unica.

### Gatilho SHORT

- Fechamento abaixo da fronteira ou rompimento seguido de reteste valido.
- Testar separadamente OI negativo, neutro e positivo.
- Delta/slope de LSR positivo.
- Aceleracao de negocios e volume; taker vendedor confirmando.

### Risco e saida

- Stop por invalidacao estrutural mais buffer de volatilidade, nunca apenas percentual
  global.
- Tamanho derivado da distancia real do stop e limite de risco da conta.
- Parcial opcional; restante conduzido pela estrutura, ATR e deterioracao de OI/LSR.
- Registrar MFE, MAE e resultado liquido de taxas para comparar stop, reteste e saida.

### Modelo separado: falso rompimento/reversao

- Exigir extensao minima alem da fronteira, seguida de fechamento de volta ao range.
- Confirmar perda da microestrutura da tentativa de rompimento ou reclaim do nivel.
- Exigir deterioracao do fluxo da direcao original; OI/LSR entram como contexto, nao
  como inversor automatico de direcao.
- Entrada no reteste/rejeicao da fronteira recuperada, com stop alem do extremo do sweep
  mais buffer de volatilidade.
- Proibir reversao imediata apenas porque o primeiro trade foi stopado.

## Criterio para promover a estrategia

- Backtest sem lookahead e com timestamps alinhados.
- Custos e slippage pessimistas.
- Separacao temporal treino/validacao/teste.
- Resultado positivo por regime e nao apenas agregado.
- Numero suficiente de trades e intervalo de confianca.
- Paper/Testnet antes de qualquer consideracao de modo real.
