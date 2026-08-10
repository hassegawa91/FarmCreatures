# V10 OI Breakout Retest

Reconstrucao limpa do bot. Esta base possui uma unica estrategia, isolada dos adaptadores de execucao `SHADOW`, `TESTNET` e `REAL`.

## Hipotese

O bot procura:

1. rompimento de uma faixa recente;
2. movimento de preco acompanhado por expansao de OI em contratos;
3. volume e agressao taker alinhados;
4. ausencia de crowding extremo em LSR e funding;
5. pullback controlado;
6. retomada com o contexto de derivativos ainda valido.

Nao existe score agregado. Cada condicao e booleana e auditavel. O unico setup chama-se `OI_BREAKOUT_RETEST`.

## O que deliberadamente nao existe

- estrategia diferente entre Real e Testnet;
- Telegram como gatilho;
- IA, auto-calibracao ou aprendizado online;
- Bollinger, RSI, MACD ou dezenas de engines;
- `smart money score`, `whale score` ou votos duplicados;
- compatibilidade com configuracoes antigas.

## Executar

```powershell
python main.py
```

Painel: `http://127.0.0.1:8000`

Testes:

```powershell
python -m unittest discover -s tests -v
```

Replay curto com ate 500 candles de 5 minutos por ativo:

```powershell
python tools\backtest.py BTCUSDT ETHUSDT SOLUSDT --limit 500
```

## Dados

Os sinais, resultados shadow e auditoria de execucao ficam em `data/v10.sqlite`. Custos de entrada e saida sao descontados.

## Modos de execucao

- `SHADOW`: padrao; nao carrega chaves e nunca envia ordens.
- `TESTNET`: usa a mesma estrategia, sizing e protecoes no endpoint Demo.
- `REAL`: usa exatamente o mesmo pipeline no endpoint Real.

Para Testnet, altere `mode` para `TESTNET` e `execution.enabled` para `true`. Para Real, alem dessas duas alteracoes, a variavel externa `V10_REAL_TRADING_CONFIRM` precisa ser exatamente `ENABLE_REAL_TRADING`. Sem as tres condicoes o processo falha fechado antes de iniciar.

O executor:

1. rejeita Hedge Mode e posicao duplicada;
2. valida drift do preco executavel;
3. dimensiona pela menor quantidade entre risco da conta e teto de margem;
4. configura margem isolada e alavancagem;
5. abre a mercado;
6. cria primeiro o stop `STOP_MARKET` e depois o alvo `TAKE_PROFIT_MARKET`, ambos `closePosition`;
7. fecha imediatamente a posicao se qualquer protecao falhar.

## Legado

O projeto anterior foi encerrado e removido de `C:\v10`. Backup sem `.env`:

`C:\Users\BOT1\Documents\v10_legacy_backup_20260728_0948.zip`

Fonte antiga em quarentena recuperavel:

`C:\Users\BOT1\Documents\v10_legacy_source_20260728_0948`
