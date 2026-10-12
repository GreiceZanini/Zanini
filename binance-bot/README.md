# binance-bot

Automação de compra e venda **spot** na Binance em Python, com:

- compra a mercado quando o preço atinge um gatilho informado na hora de rodar;
- **stop-loss fixo** abaixo do preço de compra;
- **trailing stop** para realizar lucro: arma quando o lucro mínimo é atingido, acompanha a máxima e vende quando o preço recua um percentual ou valor a partir dela (vende na reversão, não no primeiro alvo);
- venda pelo que acionar primeiro (stop-loss ou trailing);
- após a venda, observa uma **lista de moedas seguintes**, cada uma com seu gatilho, e compra a primeira que atingir o gatilho, ignorando as demais;
- preços em tempo real via **WebSocket** da Binance (stream `@trade`);
- parada segura com Ctrl+C (cancela ordens pendentes e informa a posição aberta);
- credenciais somente por **variáveis de ambiente**;
- log de cada ordem com preço, quantidade, taxa e horário (`data/bot.log` e `data/trades_<modo>.csv`);
- **modo simulação** (sem ordens reais), **testnet** oficial da Binance e **replay offline** para testar a lógica.

Biblioteca usada: [python-binance](https://github.com/sammchardy/python-binance). A `binance-connector` foi
marcada como deprecada pela própria Binance, por isso não foi adotada.

## Instalação

```bash
cd binance-bot
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # preencha as chaves; o .env está no .gitignore
```

Variáveis reconhecidas (o script carrega o `.env` automaticamente, sem sobrescrever o ambiente):

| Variável | Uso |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | produção (`--mode live`) |
| `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` | Spot Testnet (`--mode testnet`); se ausentes, usa as de produção |

Na Binance, crie a chave com permissão apenas de **Spot Trading** (sem saque) e, se possível, restrita ao IP da máquina.
Chaves da testnet: https://testnet.binance.vision.

## Uso

Exemplo completo (simulação, sem chave de API):

```bash
python binance_bot.py \
  --mode simulate \
  --symbol BTCUSDT --buy-trigger 60000 \
  --quote-amount 50 \
  --stop-loss-pct 2 \
  --profit-activation-pct 3 --trail-pct 1 \
  --next ETHUSDT:3000 --next SOLUSDT:150
```

Leitura: compra 50 USDT de BTC quando o preço atingir 60000; stop-loss em 58800 (−2%); quando o preço chegar a 61800 (+3%)
o trailing é armado e passa a vender se o preço recuar 1% a partir da maior cotação atingida. Após vender, observa ETH e SOL
e compra a que atingir seu gatilho primeiro, aplicando as mesmas regras de saída.

Parâmetros principais:

| Parâmetro | Descrição |
|---|---|
| `--mode simulate\|testnet\|live` | padrão `simulate`. `live` pede confirmação digitando `CONFIRMO` (pule com `--yes`) |
| `--symbol`, `--buy-trigger` | moeda inicial e preço de gatilho |
| `--buy-direction auto\|below\|above` | `auto` (padrão): compra quando o preço **atinge** o gatilho vindo de onde estiver (se está acima, espera cair até ele; se está abaixo, espera subir). `below`/`above` fixam a direção |
| `--quote-amount` | valor de cada compra na moeda de cotação (ex.: USDT) |
| `--stop-loss-pct` ou `--stop-loss-abs` | distância do stop-loss abaixo do preço de compra |
| `--profit-activation-pct` ou `--profit-activation-abs` | lucro mínimo que arma o trailing |
| `--trail-pct` ou `--trail-abs` | recuo a partir da máxima que dispara a venda |
| `--next SIMBOLO:PRECO` | moeda seguinte com gatilho; repita para várias |
| `--repeat` | após vender a moeda seguinte, volta a observar a lista indefinidamente (padrão: encerra) |
| `--sell-on-stop` | ao parar com Ctrl+C, vende a mercado a posição aberta |
| `--resume` | retoma a posição/fase salva em `data/state_<modo>.json` (após queda ou parada) |
| `--data-dir` | pasta de log, operações e estado (padrão `data/`) |

Todos os símbolos de uma rodada devem usar a mesma moeda de cotação (ex.: todos em USDT).

### Testar a lógica sem rede (replay)

```bash
python binance_bot.py --mode simulate --replay examples/replay_exemplo.csv \
  --symbol BTCUSDT --buy-trigger 60000 --quote-amount 100 \
  --stop-loss-pct 2 --profit-activation-pct 3 --trail-pct 1 \
  --next ETHUSDT:3000 --next SOLUSDT:150
```

O CSV tem uma linha `SIMBOLO,PRECO` por tick. No replay são usados filtros genéricos (sem consulta à Binance).

### Testnet

```bash
python binance_bot.py --mode testnet --symbol BTCUSDT --buy-trigger 60000 --quote-amount 50 \
  --stop-loss-pct 2 --profit-activation-pct 3 --trail-pct 1 --next ETHUSDT:3000
```

Envia ordens reais para a Spot Testnet (saldo fictício). Os preços da testnet podem divergir dos de produção.

### Parar com segurança

Ctrl+C (ou `SIGTERM`): o bot fecha o WebSocket, cancela qualquer ordem pendente dos símbolos envolvidos
(nos modos testnet/live), vende a posição se `--sell-on-stop` foi passado, salva o estado e imprime um aviso
com a posição que permanece aberta. Para retomar depois, rode o mesmo comando com `--resume`.

## Arquivos gerados (`data/`)

- `bot.log`: log completo (console e arquivo), com horário local.
- `trades_<modo>.csv`: uma linha por ordem executada: horário UTC e local, símbolo, lado, motivo
  (`gatilho`, `STOP_LOSS`, `TRAILING_STOP`, `STOP_MANUAL`), quantidade, preço médio, total, taxa, id da ordem e resultado da venda.
- `state_<modo>.json`: estado atual para `--resume`.

## Como as ordens são executadas

- Compra: ordem `MARKET` por valor em quote (`quoteOrderQty`). Preço de entrada = preço médio executado.
- Venda: ordem `MARKET` pela quantidade líquida recebida (descontada a taxa paga no ativo), arredondada ao `stepSize`
  da Binance e limitada ao saldo livre. São verificados `minQty` e `minNotional` antes de enviar.
- Stop-loss e trailing são monitorados **pelo bot**, não são ordens na corretora. Se a máquina ou a rede caírem,
  não há proteção até o bot voltar. Mantenha o processo rodando em máquina estável e use `--resume` ao reiniciar.
- Falha de venda (rede, API): o bot registra o erro e tenta de novo a cada `--order-retry-seconds` até conseguir.
  Falha de compra: até 3 tentativas, depois encerra com erro.
- Se o recuo do trailing for maior ou igual ao lucro de ativação, o bot avisa: a venda pelo trailing pode sair abaixo
  do preço de compra (o stop-loss continua como piso).

## Limitações realistas

- Ordem a mercado: em movimentos bruscos o preço executado pode ficar pior que o nível do stop (slippage).
- O gatilho é avaliado pelo último preço negociado (`@trade`). Em pares pouco líquidos pode haver intervalos sem trades;
  o watchdog consulta o preço via REST e reconecta se o stream ficar mudo por `--watchdog-seconds`.
- Este código não foi executado contra a Binance real durante o desenvolvimento (ambiente sem acesso à API).
  Teste primeiro em `--mode simulate`, depois `--mode testnet`, e só então `--mode live` com valores pequenos.

## Testes

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests
```

Os testes cobrem gatilhos (auto/explícito), stop-loss, armar/subir trailing, venda na reversão, lista de moedas
seguintes, repetição, taxas, falhas de ordem, salvar/retomar estado e o parse das respostas de ordem da Binance.
