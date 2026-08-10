# Checkpoint da pesquisa Telegram — Encryptos

## Lote inicial congelado

- Checkpoint: `encryptos-20260803T225828Z-p107`
- Paginas: `messages.html` ate `messages107.html`
- Periodo das mensagens: 29/11/2022 12:14:35 ate 06/02/2023 23:34:18 (UTC-03:00)
- Blocos de mensagem indexados: 107.176
- Mensagens com texto: 92.637
- Mensagens com midia: 17.299
- Midias referenciadas, presentes e hasheadas: 33.265
- Volume protegido por hash: 4.727.771.103 bytes
- Arquivos ausentes ou vazios: 0

## Arquivos de controle

- Manifesto corrente: `data/telegram_research/checkpoints/latest_checkpoint.json`
- Manifesto imutavel do lote: `data/telegram_research/checkpoints/encryptos-20260803T225828Z-p107.json`
- Ledger de processamento: `data/telegram_research/checkpoints/analysis_ledger.jsonl`
- Indice pesquisavel: `data/telegram_research/encryptos_research.sqlite`

O manifesto usa SHA-256 para cada pagina e cada midia. O ledger registra separadamente
cada etapa concluida para o hash. Portanto, um arquivo renomeado ou encontrado em um
novo lote nao volta a ser processado se seu conteudo ja tiver sido analisado.

## Estado do processamento

- As 107 paginas HTML estao em `TEXT_INDEXED`.
- As 33.265 midias estao em `SNAPSHOTTED`; analise visual, transcricao ou extracao de
  documentos sera registrada em etapas proprias conforme for executada.
- Uma segunda execucao do indexador foi validada: 107 paginas ignoradas e zero
  mensagens duplicadas.

## Continuidade apos a exportacao

1. Gerar outro checkpoint da pasta exportada.
2. Comparar os hashes com `processed_hashes`/ledger.
3. Indexar apenas paginas com hash ainda nao registrado.
4. Processar apenas midias sem a etapa correspondente no ledger.
5. Consolidar as hipoteses de mercado separando observacao, regra testavel e resultado.

O Telegram e tratado como fonte de hipoteses. Nenhuma mensagem ou sinal deve acionar
ordens diretamente sem validacao quantitativa independente.
