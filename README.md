# Prospector: empresas sem site próprio

Busca negócios por tipo e localização no Google (Places API oficial) e gera um CSV com as empresas que não têm site próprio, com telefone, endereço, link do Google Maps e @ do Instagram quando cadastrado.

## Como a empresa é classificada

| situacao_site | Significado |
|---|---|
| sem_site | Nenhum link cadastrado no Google |
| instagram, facebook, whatsapp, linktree, google, ... | Só tem rede social / página gratuita, sem domínio próprio |
| site_proprio | Tem site próprio (fica fora da lista) |

## Pré-requisitos

1. Python 3.10+ (sem dependências externas).
2. Chave da Google Maps Platform com a Places API (New) ativada: https://developers.google.com/maps/documentation/places/web-service/get-api-key

## Uso

```bash
export GOOGLE_MAPS_API_KEY="sua_chave"

# Um ou mais tipos de negócio em uma cidade
python -m prospector --tipo "padaria" --tipo "salão de beleza" --local "Toledo, PR"

# Por bairro, para passar do limite de 60 resultados por busca
python -m prospector --tipo "oficina mecânica" --local "Cascavel, PR" --bairro Centro --bairro "Santa Cruz"

# Apenas quem não tem nenhum link e tem telefone
python -m prospector --tipo "pet shop" --local "Toledo, PR" --somente-sem-link --exigir-telefone --saida petshops.csv
```

O CSV sai com separador `;` e codificação UTF-8 com BOM (abre direto no Excel).

## Limitações (realistas)

- Google: a API retorna no máximo 60 resultados por busca. Use `--bairro` para cobrir a cidade inteira.
- Custo: os campos telefone e site são cobrados na faixa Enterprise da Places API. Consulte a tabela oficial antes de rodar em volume: https://developers.google.com/maps/billing-and-pricing/pricing
- Instagram: a Meta não oferece API oficial para buscar empresas por localização (a Business Discovery da Graph API só consulta um @ já conhecido). Por isso o Instagram entra pelo link que a própria empresa cadastrou no Google. Raspagem do Instagram viola os termos de uso e não foi implementada.
- Web em geral: não foi incluída busca genérica na web; a Custom Search JSON API do Google não aceita novos clientes. O Google Maps já concentra a maior parte dos negócios locais.
- Uso dos dados: contato comercial com empresas deve respeitar a LGPD (base legal de legítimo interesse, opção de descadastro).

## Testes

```bash
python -m unittest discover -s tests
```
