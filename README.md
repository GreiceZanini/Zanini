# Prospector: empresas sem site próprio

Busca negócios por localização e gera um CSV (abre no Excel) com as empresas que não têm site próprio, com os contatos disponíveis.

## Fontes

| Fonte | Custo | O que traz | Sabe se tem site? |
|---|---|---|---|
| `osm` OpenStreetMap | Gratuito, sem cadastro | Nome, categoria, telefone, WhatsApp, e-mail, Instagram, Facebook, endereço | Sim (campo website) |
| `cnpj` Dados Abertos do CNPJ (Receita Federal) | Gratuito | CNPJ, razão social, nome fantasia, CNAE, telefones, e-mail, endereço | Não; usa o e-mail como indício |
| `google` Google Places API | Cota gratuita mensal, exige cartão cadastrado | Nome, telefone, site, Instagram cadastrado, avaliação | Sim |

Na prática: o OSM tem cobertura irregular em cidades menores (depende de voluntários); a base do CNPJ tem todas as empresas formais, mas não informa site; o Google é o mais completo.

## Uso

Python 3.10+, sem dependências externas.

### OpenStreetMap (gratuito)

```bash
python -m prospector osm --local "Toledo, PR"
python -m prospector osm --local "Toledo, PR" --categoria shop=bakery --categoria amenity=restaurant --exigir-telefone
```

Categorias seguem as tags do OSM: https://wiki.openstreetmap.org/wiki/Map_features (ex.: `shop=hairdresser`, `shop=car_repair`, `amenity=cafe`, `craft`).

### CNPJ (gratuito)

1. Baixe no portal oficial (https://dados.gov.br, conjunto "Cadastro Nacional da Pessoa Jurídica - CNPJ") os arquivos `Estabelecimentos0..9.zip`, `Empresas0..9.zip` e `Municipios.zip` numa pasta. Não precisa descompactar.
2. Rode:

```bash
python -m prospector cnpj --dados ./cnpj --municipio Toledo --uf PR --cnae 1091 --cnae 9602
```

Classificação pelo e-mail cadastrado na Receita:

| situacao_site | Significado |
|---|---|
| email_gratuito | Usa gmail, hotmail etc.: provável sem site |
| sem_email | Sem e-mail cadastrado |
| dominio_sem_site | E-mail com domínio próprio, mas o domínio não tem site no ar (com `--verificar-dominio`) |

`--cnae` aceita prefixo do CNAE principal (lista oficial: https://concla.ibge.gov.br). O processamento lê alguns GB e pode levar vários minutos.

### Google Places (cota gratuita)

```bash
export GOOGLE_MAPS_API_KEY="sua_chave"
python -m prospector google --tipo padaria --local "Toledo, PR" --max-buscas 50
```

Os campos telefone e site estão no nível Enterprise da Places API, com 1.000 chamadas gratuitas por mês (cada chamada traz até 20 empresas). Acima disso é cobrado. `--max-buscas` limita as chamadas por execução. Preços oficiais: https://developers.google.com/maps/billing-and-pricing/pricing

## Limitações

- Instagram: a Meta não oferece API oficial para buscar empresas por localização. O Instagram aparece quando a empresa o cadastrou no OSM ou no Google. Raspagem viola os termos de uso e não foi implementada.
- Uso dos dados: contato comercial deve respeitar a LGPD.

## Testes

```bash
python -m unittest discover -s tests
```
