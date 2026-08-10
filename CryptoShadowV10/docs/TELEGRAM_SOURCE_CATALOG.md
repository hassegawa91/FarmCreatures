# Catálogo operacional de fontes do Telegram

Atualizado em 03/08/2026. Toda fonte começa em `RESEARCH_ONLY`: nenhuma mensagem
externa pode enviar ordem diretamente.

| Fonte | Classe | Grupo de independência | Uso correto |
|---|---|---|---|
| Encryptos | educação/comunidade | `encryptos` | hipóteses e regras para validação histórica |
| Pumps & Dumps [public] | aceleração de preço | `pump_dump_feed` | evento reativo de impulso/exaustão; nunca entrada direta |
| Binance Liquidations | liquidações | `liquidation_feed` | clusters por ativo, lado, valor e janela de tempo |
| Alertas Cripto Futuros | aceleração de preço | `price_alert_feed` | comparação de latência e persistência do impulso |
| Binance Killers Vip | sinais estruturados | `syndicated_signal_2197` | benchmark de sinal, entrada, stop e alvos |
| Bitcoin Assassins© | sinais estruturados | `syndicated_signal_2197` | mesma família republicadora; não soma consenso |
| Bitcoin BOOM Signals | sinais estruturados | `syndicated_signal_2197` | mesma família republicadora; não soma consenso |
| Crypto Sharks® | sinais/análises | `crypto_sharks` | pendente de exportação e avaliação |
| Bullish Traders® | análise técnica | `bullish_traders` | hipóteses de estrutura/reteste, sem execução direta |
| Bitcoin Magazine / Mundstock | macro/institucional | `macro_news` | regime e risco de evento; nunca gatilho isolado |

## Estrutura funcional

1. `REGIME`: tendência e volatilidade de BTC, breadth do mercado, funding e agenda macro.
2. `EVENTO`: cluster de liquidação, aceleração de preço/volume, mudança de OI/LSR e taker.
3. `SETUP`: continuação após sustentação/reteste ou reversão após clímax e falha de continuação.
4. `EXECUÇÃO`: preço público de produção em tempo real; corretora Testnet ou Real selecionável.
5. `ATRIBUIÇÃO`: cada trade registra quais eventos contribuíram e o estado observado.

O feed de pump/dump observado é reativo: ele informa movimentos já ocorridos a cada cinco
minutos. O valor está na sequência de estados. Exemplo visto em BANK: alertas crescentes de
pump (`+6,42%`, `+10%`, `+11,58%`) e depois uma cascata de dump (`-7,48%`, `-9,98%`,
`-7,6%`). A hipótese testável é distinguir continuação de clímax por preço, OI, taker,
volume, liquidações e falha de máxima/mínima; copiar o último alerta tende a chegar atrasado.

## Critério para uma fonte influenciar a engine

- timestamp verificável e deduplicação entre republicadores;
- no mínimo 200 eventos válidos e mais de um regime de mercado;
- avaliação em 5, 15, 30, 60 e 180 minutos usando preço de produção;
- MFE, MAE, retorno líquido, taxa, slippage e latência contabilizados;
- resultado fora da amostra e estabilidade por ativo/semana;
- Testnet somente depois de a hipótese superar o benchmark sem o evento externo.
