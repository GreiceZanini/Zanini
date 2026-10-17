# binance-bot

Automação de compra e venda **spot** na Binance em Python, com várias moedas em paralelo. Para cada moeda você informa:

- preço de gatilho da compra e valor da compra **em reais** (convertido pela cotação da moeda de cotação do par,
  ex.: USDT/BRL, no momento da compra);
- na queda, **stop-loss**: em R$ (perda máxima na operação), em % ou em preço;
- na alta, **mínimo para venda**: um preço de venda (ex.: 0,071) ou um lucro mínimo em R$ (ex.: R$ 2,00), ou %;
- **trailing**: recuo a partir da máxima (em % ou R$) que dispara a venda, nunca abaixo do mínimo para venda.

Todas as moedas são observadas ao mesmo tempo; pode haver várias posições abertas em paralelo. Depois de vender, a
moeda volta a esperar o gatilho de compra, até o bot ser desligado.

Também inclui: preços em tempo real via **WebSocket**, parada segura, chaves só por variáveis de ambiente, log de cada
ordem com preço e horário, modo **simulação**, **testnet**, replay offline e a página web local **Sistema**.

Biblioteca usada: [python-binance](https://github.com/sammchardy/python-binance). A `binance-connector` foi
marcada como deprecada pela própria Binance, por isso não foi adotada.

## Instalação

```bash
cd binance-bot
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # preencha as chaves; o .env está no .gitignore
```

| Variável | Uso |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | produção (`--mode live`) |
| `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` | Spot Testnet (`--mode testnet`); se ausentes, usa as de produção |

A simulação não usa chave. Na Binance, crie a chave com permissão apenas de **Spot Trading** (sem saque) e, se possível,
restrita ao IP da máquina. Chaves da testnet: https://testnet.binance.vision.

## Página "Sistema" (interface web local)

```bash
python sistema.py            # abre http://127.0.0.1:8080
```

- Iniciar: modo (simulação, testnet, live com confirmação) e a lista de moedas, cada uma com símbolo, gatilho, direção,
  valor em R$, stop-loss, mínimo para venda e recuo do trailing, cada campo com explicação em português.
- Alimentar: com o bot rodando, inclui ou atualiza moedas (níveis das posições abertas são recalculados), substitui a
  lista ou remove moedas sem posição aberta.
- Parar: mostra as posições abertas e para com segurança, vendendo ou não as posições.
- Ao lado: carteira (capital alocado + realizado + posições abertas) com variação por período (1h, 4h, 1d, 15d),
  moedas em posição primeiro, cada uma com preço e variação de 24h, gatilho ou venda mínima, detalhes ao passar o
  mouse na seta (compra, níveis, ordens e resultado), tabela de saldos por moeda e log (completo em `/api/log`).

O servidor escuta só em 127.0.0.1 e não tem autenticação; não exponha na rede.

## Linha de comando

Um plano por moeda, entre aspas, repetindo `--plan` para várias:

```bash
python binance_bot.py --mode simulate \
  --plan "SUSDT trigger=0.067 amount=50 stop=money:1 activation=price:0.071 trail=pct:0.8" \
  --plan "ETHBRL trigger=20000 amount=50 stop=money:1 activation=money:2 trail=pct:0.8"
```

Leitura do primeiro plano: compra R$ 50 de S (par SUSDT, convertido pela cotação USDT/BRL) quando o preço atingir
0,067; vende se perder R$ 1,00; ao atingir 0,071 passa a seguir a alta e vende quando recuar 0,8% a partir da máxima,
nunca abaixo de 0,071. Se o recuo de 0,8% ficar abaixo de 0,071, a venda ocorre em 0,071.

| Chave do plano | Unidades | Significado |
|---|---|---|
| `trigger` | preço | gatilho de compra |
| `amount` | R$ | valor da compra em reais |
| `stop` | `money:R$` · `pct:%` · `price:preço` | stop-loss abaixo do preço de compra |
| `activation` | `price:preço` · `money:R$` · `pct:%` | mínimo para venda na alta (piso do trailing) |
| `trail` | `pct:%` · `money:R$` | recuo a partir da máxima que dispara a venda |
| `direction` | `auto` · `below` · `above` | `auto` compra quando o preço atinge o gatilho vindo de onde estiver |

Os planos também podem vir de um JSON (`--config examples/plano_exemplo.json`, mesmo formato salvo pela página).

Opções: `--no-sell-on-stop` (ao parar, mantém as posições), `--resume` (retoma posições salvas), `--yes` (sem
confirmação no live), `--data-dir`, `--watchdog-seconds`, `--order-retry-seconds`.

### Testar a lógica sem rede (replay)

```bash
python binance_bot.py --mode simulate --replay examples/replay_exemplo.csv --config examples/plano_exemplo.json
```

O CSV tem uma linha `SIMBOLO,PRECO` por tick. Para pares em USDT inclua linhas `USDTBRL,<cotação>`. No replay são usados
filtros genéricos, sem consulta à Binance.

### Testnet e live

`--mode testnet` envia ordens reais à Spot Testnet (saldo fictício; preços podem divergir dos de produção).
`--mode live` pede para digitar CONFIRMO e opera com dinheiro real. Teste nesta ordem: simulação, testnet, live com
valores pequenos.

### Parar com segurança

Ctrl+C (ou `SIGTERM`): o bot fecha o WebSocket, cancela ordens pendentes dos símbolos (testnet/live), vende as
posições abertas (padrão; `--no-sell-on-stop` mantém), salva o estado e lista o que ficou aberto. Para retomar depois,
rode de novo com `--resume`.

## Arquivos gerados (`data/`)

- `bot.log`: log completo, com horário local.
- `trades_<modo>.csv`: uma linha por ordem executada: horário UTC e local, símbolo, lado, motivo (`gatilho`,
  `STOP_LOSS`, `TRAILING_STOP`, `MINIMO_VENDA`, `STOP_MANUAL`), quantidade, preço médio, total na moeda de cotação,
  cotação usada, total em R$, taxa, id da ordem e resultado da venda em R$ e %.
- `state_<modo>.json`: posições, planos e estatísticas por moeda para `--resume`.
- `equity_<modo>.csv`: valor da carteira a cada minuto (últimos 15 dias), base da variação por período.

## Como as ordens e os níveis funcionam

- Compra: ordem `MARKET` por valor na moeda de cotação (`quoteOrderQty`), calculado a partir dos reais informados e da
  cotação atual (ex.: USDTBRL). Preço de entrada = preço médio executado.
- Venda: ordem `MARKET` pela quantidade líquida recebida (descontada a taxa paga no ativo), arredondada ao `stepSize`
  e limitada ao saldo livre. São verificados `minQty` e `minNotional` antes de enviar.
- Regras em R$ são convertidas em distância de preço: R$ ÷ cotação ÷ quantidade comprada. A cotação usada é a do
  momento da compra; o resultado em R$ de cada venda usa a cotação do momento da venda.
- Trailing: arma quando o preço atinge o mínimo para venda; a partir daí o nível de venda é o maior entre
  "máxima − recuo" e o mínimo. A venda só dispara depois de uma reversão (preço abaixo da máxima), nunca no instante em
  que o mínimo é tocado. A ordem é a mercado: em quedas rápidas o preço executado pode ficar um pouco abaixo do nível.
- Stop-loss em preço precisa ficar abaixo do gatilho e mínimo em preço acima dele; o bot recusa o plano caso contrário
  (um stop acima do preço de compra venderia no mesmo instante e recompraria em seguida, sem parar).
- Stop-loss e trailing são monitorados **pelo bot**, não são ordens na corretora. Se a máquina ou a rede caírem, não há
  proteção até o bot voltar. Mantenha o processo rodando em máquina estável e use `--resume` ao reiniciar.
- Falha de venda: registra e tenta de novo a cada `--order-retry-seconds` até conseguir. Falha de compra: até 3
  tentativas, depois só aquela moeda é desativada; as outras continuam.

## Limitações realistas

- O gatilho é avaliado pelo último preço negociado (`@trade`). Em pares pouco líquidos pode haver intervalos sem trades;
  o watchdog consulta o preço via REST e reconecta se o stream ficar mudo por `--watchdog-seconds`.
- Pares em BRL têm menos liquidez que os em USDT; em valores pequenos isso não costuma atrapalhar.
- Este código não foi executado contra a Binance real durante o desenvolvimento (ambiente sem acesso à API).

## Testes

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests
```

Cobrem conversão de reais, níveis em R$/%/preço, piso do trailing, várias moedas em paralelo, recompra após venda,
alimentar (incluir, atualizar, substituir, remover), falhas de ordem, salvar/retomar estado, parse das respostas da
Binance, feed/loop e a página Sistema (iniciar, alimentar, parar e API HTTP).
