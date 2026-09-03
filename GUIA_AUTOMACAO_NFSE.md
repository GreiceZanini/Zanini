# Guia completo: automação de download XML NFS-e (Recebidas/Emitidas)

Este guia descreve **todos os passos**, com os **aplicativos necessários** e **comandos exatos**, para executar uma automação que:

1. Acessa a página de Notas (sessão já autenticada).
2. Seleciona tipo de nota (`Recebidas` ou `Emitidas`).
3. Preenche `Data Inicial` e `Data Final` no formato `ddmmaaaa` para cada mês do período.
4. Clica em `Filtrar`.
5. Percorre as linhas da listagem e baixa XML em `...` → `Download XML`.
6. Repete mês a mês até concluir o período solicitado.

---

## 1) Aplicativos necessários

### 1.1 Obrigatórios

- **Python 3.10+**
- **pip** (gerenciador de pacotes Python)
- **Playwright** (automação do navegador)
- **Navegador Chromium do Playwright**

### 1.2 Opcional (recomendado)

- **venv** (ambiente virtual Python, já incluído normalmente)

---

## 2) Estrutura esperada do projeto

```text
/workspace/Zanini
├── GUIA_AUTOMACAO_NFSE.md
└── scripts/
    └── baixar_nfse_xml.py
```

---

## 3) Instalação passo a passo (Linux/macOS)

> Execute os comandos abaixo na pasta do projeto (`/workspace/Zanini`).

### 3.1 Verificar versões instaladas

```bash
python3 --version
pip3 --version
```

### 3.2 Criar e ativar ambiente virtual

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3.3 Atualizar pip (boa prática)

```bash
python -m pip install --upgrade pip
```

### 3.4 Instalar Playwright

```bash
pip install playwright
```

### 3.5 Instalar o navegador Chromium controlado pelo Playwright

```bash
python -m playwright install chromium
```

---

## 4) Instalação passo a passo (Windows PowerShell)

### 4.1 Verificar versões

```powershell
py --version
pip --version
```

### 4.2 Criar e ativar ambiente virtual

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 4.3 Atualizar pip

```powershell
python -m pip install --upgrade pip
```

### 4.4 Instalar Playwright e Chromium

```powershell
pip install playwright
python -m playwright install chromium
```

---

## 5) Como preparar autenticação (importantíssimo)

O script pressupõe sessão autenticada no portal da NFS-e.

Estratégias possíveis:

1. **Perfil já autenticado** (mais simples para operação assistida).
2. **Storage state do Playwright** (persistir cookies/sessão).

Se o portal expirar sessão com frequência, execute com janela visível (`--headed`) para acompanhar.

---

## 6) Execução da automação

### 6.1 Exemplo para notas recebidas

```bash
python scripts/baixar_nfse_xml.py \
  --tipo recebidas \
  --inicio 2024-01-01 \
  --fim 2024-12-31 \
  --download-dir ./downloads \
  --headed
```

### 6.2 Exemplo para notas emitidas

```bash
python scripts/baixar_nfse_xml.py \
  --tipo emitidas \
  --inicio 2023-01-01 \
  --fim 2023-06-30 \
  --download-dir ./downloads_emitidas \
  --headed
```

### 6.3 Parâmetros disponíveis

- `--tipo`: `recebidas` ou `emitidas`
- `--inicio`: data inicial (`YYYY-MM-DD`)
- `--fim`: data final (`YYYY-MM-DD`)
- `--download-dir`: pasta de destino dos XML
- `--timeout-ms`: timeout por ação (padrão: `30000`)
- `--headed`: abre navegador visível

---

## 7) Como o script executa cada mês (lógica detalhada)

Para cada mês dentro do intervalo informado:

1. Define início/fim do mês (recortado ao período total pedido).
2. Seleciona tipo de nota (`Recebidas`/`Emitidas`).
3. Preenche:
   - Data Inicial: `ddmmaaaa`
   - Data Final: `ddmmaaaa`
4. Clica em **Filtrar**.
5. Aguarda recarregamento da listagem.
6. Para cada linha da tabela:
   - Clica em `...`
   - Clica em `Download XML`
   - Salva arquivo no diretório escolhido.
7. Se houver paginação, processa próxima página e repete.
8. Ao terminar o mês, avança para o mês seguinte.

---

## 8) Ajuste de seletores (quando necessário)

Sites governamentais podem mudar HTML/atributos. Se algo falhar, ajuste seletores no arquivo `scripts/baixar_nfse_xml.py`.

Seletores mais sensíveis:

- Rádio tipo nota: `get_by_role("radio", name="Recebidas"|"Emitidas")`
- Data inicial/final: `input[placeholder='Data Inicial']` e `input[placeholder='Data Final']`
- Botão filtrar: `get_by_role("button", name="Filtrar")`
- Tabela: `table tbody tr`
- Menu de ação: botão `...`
- Opção de menu: `Download XML`

Para descobrir seletores corretos de forma prática:

```bash
python -m playwright codegen https://www.nfse.gov.br/EmissorNacional/Notas/Recebidas
```

---

## 9) Tratamento de erros recomendado

- Se der timeout em um mês, registrar no log e seguir para o próximo mês.
- Se um XML falhar, tentar novamente (retry).
- Gerar relatório final por mês (quantidade de XML baixados).

---

## 10) Checklist operacional

1. Ambiente virtual ativo.
2. Dependências instaladas.
3. Sessão autenticada válida no portal.
4. Período conferido (`--inicio`/`--fim`).
5. Tipo correto (`recebidas` ou `emitidas`).
6. Pasta de download com permissão de escrita.
7. Execução com `--headed` para monitoramento.

---

## 11) Comandos rápidos (copiar e colar)

### Linux/macOS

```bash
cd /workspace/Zanini
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install playwright
python -m playwright install chromium
python scripts/baixar_nfse_xml.py --tipo recebidas --inicio 2024-01-01 --fim 2024-12-31 --download-dir ./downloads --headed
```

### Windows PowerShell

```powershell
cd C:\caminho\para\Zanini
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install playwright
python -m playwright install chromium
python scripts\baixar_nfse_xml.py --tipo recebidas --inicio 2024-01-01 --fim 2024-12-31 --download-dir .\downloads --headed
```

