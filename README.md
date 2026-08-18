# VFX Preview Builder

Projeto independente do Discord bot para pré-gerar as páginas animadas do **VFX Studio** em GitHub Actions e hospedar os GIFs em **GitHub Releases**.

O bot Discord não faz parte deste repositório e não é alterado por este projeto.

## Arquitetura

```text
VFXData.json
↓
GitHub Actions
↓
Python + Pillow
↓
GIFs compostos 2×3
↓
GitHub Release pública
↓
URLs HTTPS públicas
```

Não usa Cloudflare R2, cartão, boto3, cookies Roblox ou `.ROBLOSECURITY`.

## O que é gerado

Cada página contém até **6 flipbooks**, em **2 colunas × 3 linhas**.

Cada célula mantém:

```text
Texture ID
[preview animado]
```

A versão atual usa um layout de galeria em tema escuro, com cabeçalho discreto, cards arredondados e IDs desenhados de forma que não sejam cortados.

Medidas atuais do layout:

- **2 colunas × 3 linhas**;
- **696 × 958 px** por página;
- cards com **326 px de largura**;
- cabeçalho do card separado da área preta do preview.

O builder preserva:

- grids 2×2, 4×4 e 8×8;
- ordem dos frames linha → coluna;
- proporção do efeito;
- placeholder individual quando um asset falha;
- velocidade adaptativa de 12 / 20 / 30 FPS;
- no máximo 64 frames na timeline composta.

A timeline não usa MMC. Cada VFX simplesmente usa `tick % quantidade_de_frames` dentro de uma timeline comum limitada.

## Releases usadas

O modo de teste usa uma Release separada:

```text
vfx-previews-test
```

O modo completo usa:

```text
vfx-previews
```

Assim um teste não altera a biblioteca definitiva.

Os GIFs recebem nomes imutáveis com hash dos dados da página e hash real do arquivo gerado, por exemplo:

```text
page--fire-xxxxxxxx--001--<hash-da-pagina>--<hash-do-gif>.gif
```

Isso evita depender de sobrescrever um GIF mantendo a mesma URL.

## Manifest

Ao final de uma execução bem-sucedida, o builder envia um manifest imutável com nome semelhante a:

```text
manifest--test--<hash>.json
```

ou:

```text
manifest--full--<hash>.json
```

O manifest inclui, entre outros dados:

- fonte;
- versão do catálogo;
- configuração/versão do gerador;
- categoria;
- quantidade de itens e páginas;
- 6 itens por página;
- Asset IDs de cada página;
- hash lógico da página;
- hash real do GIF;
- nome do asset na Release;
- URL HTTPS pública;
- FPS e quantidade de frames;
- falhas individuais.

Não existe `manifest.json` mutável. Para evitar cache velho, o manifest também é content-addressed. A versão atual é descoberta pela Release `vfx-previews-test` ou `vfx-previews`, escolhendo o `manifest--*.json` mais recente.

## Incremental

Antes de gerar:

1. o builder abre a Release correspondente;
2. lista os assets da Release em páginas de até 100;
3. baixa **somente o manifest mais recente**;
4. usa esse manifest como índice;
5. páginas limpas com hash igual e arquivo ainda presente são reaproveitadas;
6. páginas que tiveram placeholder são geradas novamente para tentar recuperar o asset;
7. páginas novas/modificadas são geradas e enviadas;
8. o novo manifest só é publicado depois que todas as páginas necessárias existem;
9. somente depois disso, assets antigos do próprio builder deixam de ser necessários e são removidos.

Não há `HEAD`/consulta HTTP individual para cada página.

## Secrets necessários

Agora existe apenas **um Secret manual**:

```text
ROBLOX_API_KEY
```

O GitHub cria automaticamente um `GITHUB_TOKEN` temporário para cada execução. Não crie PAT/token pessoal para o builder.

Nunca coloque sua `ROBLOX_API_KEY` em arquivo, commit, README ou mensagem pública.

## ROBLOX_API_KEY

Se você já possui uma chave que funciona no fluxo atual de Asset Delivery do `/ro-flipbooks`, use essa mesma chave no Secret do GitHub.

O builder envia a chave somente para:

```text
https://apis.roblox.com/asset-delivery-api/v1/assetId/<ID>
```

A URL CDN retornada pela Roblox é baixada em uma segunda requisição **sem a API Key**.

Se criar uma chave nova, configure no Creator Dashboard somente as permissões de Assets/Asset Delivery necessárias aos assets que sua conta pode acessar. A interface/documentação de scopes da Roblox pode mudar; um HTTP 403 no Actions normalmente significa que a chave não tem acesso ao asset/creator necessário.

## Configurar o GitHub pelo celular

### 1. Repositório público

O repositório precisa ser **Public**, porque o Discord/navegador deve conseguir abrir as URLs dos Release assets sem autenticação.

### 2. Cadastrar a chave Roblox

No seu repositório:

```text
Settings
→ Secrets and variables
→ Actions
→ New repository secret
```

Nome:

```text
ROBLOX_API_KEY
```

Valor: sua chave Open Cloud da Roblox.

Não precisa cadastrar `GITHUB_TOKEN`: ele é fornecido automaticamente pelo GitHub Actions.

### 3. Permissão da Release

O workflow já contém:

```yaml
permissions:
  contents: write
```

Essa é a permissão usada para criar a Release e gerenciar seus assets.

Se a sua conta/organização bloquear write tokens e o Actions responder `Resource not accessible by integration`, confira:

```text
Settings
→ Actions
→ General
→ Workflow permissions
```

Em repositório pessoal normalmente o `permissions: contents: write` do próprio workflow é suficiente.

### 4. Primeiro teste

Abra:

```text
Actions
→ Generate VFX previews
→ Run workflow
```

Escolha:

```text
mode: test
test_pages: 1
```

Não use `full` ainda.

### 5. Verificar o resultado

Se o workflow terminar verde, abra a área **Releases** do repositório.

Deve existir:

```text
VFX previews (test)
```

com tag:

```text
vfx-previews-test
```

Ela deve conter pelo menos:

- um `page--....gif`;
- um `manifest--test--....json`.

Abra o GIF pelo navegador e confira principalmente:

- cabeçalho da página;
- indicador `X/Y`;
- cards arredondados;
- IDs sem corte;
- animação dos 6 VFX;
- placeholders quando houver falha individual.

Se o GIF carregar e animar, a prova de arquitetura foi concluída:

```text
GitHub Actions → Roblox → Pillow → GitHub Releases → HTTPS
```

## Modo full

Somente depois de validar `test`:

```text
Actions
→ Generate VFX previews
→ Run workflow
→ mode: full
```

Isso usa outra Release (`vfx-previews`) e processa todas as páginas necessárias.

## Relatório do Actions

No final do job, o Step Summary mostra:

- páginas analisadas;
- páginas reaproveitadas;
- páginas geradas;
- páginas com falha;
- assets com falha;
- bytes enviados;
- GIF médio, menor e maior;
- tempo médio por página;
- tempo total;
- URL da Release;
- URL do manifest publicado.

## Limites e observações

GitHub Releases aceita até 1000 assets por Release e arquivos individuais abaixo de 2 GiB. O builder remove assets antigos do próprio gerador depois de publicar um manifest novo, para evitar crescimento indefinido.

Uma mudança no conteúdo binário de uma textura Roblox que mantenha exatamente o mesmo Asset ID e os mesmos metadados do catálogo pode não ser detectada por uma página já considerada limpa, pois detectar isso exigiria baixar novamente todos os assets e eliminaria boa parte do benefício incremental.

Os efeitos continuam respeitando alpha internamente, mas a galeria final agora usa **fundo escuro/área de preview preta** para melhor legibilidade, aparência no Discord e compressão do GIF.

## Testes locais

```bash
python -m unittest discover -s tests -v
```

Os testes não precisam de secrets nem acessam suas contas.

A entrega foi validada offline com testes sintéticos, compilação Python e verificações estruturais. Ela **não foi executada contra sua conta GitHub nem com sua ROBLOX_API_KEY**, porque essas credenciais não devem ser compartilhadas.
