# Monday Extractor

Ferramenta somente leitura — local ou hospedada no Render — para listar boards do monday.com e exportar seus itens para XLSX ou JSON, incluindo anexos, comentários (updates), subitens e colunas de conexão/espelho.

## Requisitos

- Python 3.10 ou superior
- Um Personal API token V2 do monday.com com acesso ao board

## Executar

No PowerShell, dentro desta pasta:

```powershell
python app.py
```

O navegador abrirá em `http://127.0.0.1:8765`. Para trocar a porta:

```powershell
$env:PORT=9000; python app.py
```

Cole o token na tela, carregue os boards, escolha um board e clique em **Extrair dados**. Depois use XLSX ou JSON para baixar o resultado.

Para exigir senha na entrada também no uso local, copie `.env.example` para `.env` e preencha `APP_PASSWORD` (o arquivo `.env` é lido automaticamente e não vai para o Git). Também funciona definir a variável direto no PowerShell:

```powershell
$env:APP_PASSWORD="minha-senha"; python app.py
```

## Publicar no Render (sem banco)

A aplicação é stateless — o token vem a cada requisição e nada precisa persistir — então basta um **Web Service** simples:

1. Suba estes arquivos em um repositório Git **privado** e crie um Web Service no Render apontando para ele (runtime **Python**). O `.gitignore` já impede que `.env` (senha) e `anexos/` (dados de clientes) entrem no repositório.
2. Build command: `pip install -r requirements.txt` (padrão). Start command: `python app.py`.
3. Em **Environment**, defina `APP_PASSWORD` com a senha de acesso do time. **Ela é obrigatória no Render** — a aplicação se recusa a iniciar sem senha quando hospedada.

O Render injeta `PORT` e `RENDER` automaticamente; a aplicação detecta o ambiente e passa a escutar em `0.0.0.0`, mostra a tela de senha antes de qualquer coisa e muda o botão **Baixar anexos** para gerar um ZIP baixado pelo navegador (o disco do servidor é efêmero).

Observações:

- As sessões ficam em memória: a cada deploy ou restart, todos precisam digitar a senha novamente.
- A senha é única e compartilhada — troque-a na variável de ambiente quando alguém sair do time.

## Formatos de exportação

- **XLSX** (recomendado) — gerado sem nenhuma dependência extra, abre direto no Excel sem problemas de separador ou acentuação. Traz até 4 abas: **Itens** (uma linha por tarefa, com filtro automático, cabeçalho congelado e links clicáveis), **Comentários** (uma linha por update/resposta), **Anexos** (uma linha por arquivo, com tamanho e autor) e **Subitens**. As abas de detalhe só aparecem quando a extração inclui esses dados.
- **JSON** — estrutura completa aninhada (itens, anexos com URLs, comentários, subitens), ideal para integrações ou reimportação.

## O que é extraído

Sempre: id, nome, grupo, datas, link do item (`item_url`) e todas as colunas do board. Colunas de link retornam a URL; colunas de conexão entre boards e espelho retornam os nomes dos itens ligados.

Com **Incluir anexos, comentários e subitens** marcado (padrão), cada item traz também:

- **anexos** — arquivos das colunas de arquivo e dos comentários (nome, tamanho, autor; URL completa no JSON);
- **comentarios** — updates com autor, data, respostas e arquivos anexados;
- **subitens** — subitens com suas próprias colunas.

A busca de detalhes é feita em lotes de 20 itens para respeitar o limite de complexidade da API; em boards grandes a extração demora um pouco mais.

## Baixar anexos

Após a extração, o botão **Baixar anexos** salva os arquivos em `anexos/<board>/<id do item> - <nome do item>/`, ao lado do `app.py`. No Render, em vez disso, o navegador baixa um ZIP com a mesma estrutura de pastas. As URLs públicas dos anexos expiram em cerca de 1 hora após a extração — baixe logo em seguida.

O token não é salvo em arquivo nem enviado para outro serviço além da API oficial `https://api.monday.com/v2` (o download de anexos acessa as URLs públicas retornadas por ela). A aplicação não executa mutações.
