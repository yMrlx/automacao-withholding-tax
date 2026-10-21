# Fixture real da tabela de impostos

`tabela_impostos_64_real.html` e' uma copia sanitizada do `outerHTML` da tabela
WebDynpro de Withholding Tax capturado em **24/09/2026**, durante a conferencia
das 64 linhas salvas. Nao e' uma tabela reconstruida pelo gerador de DOM sintetico
da suite.

Origem local: `logs/tabela_real_64linhas_20260924_155544.html`, ignorada pelo Git.
SHA-256 da captura original:
`68d91b1d89edcca544d3bb6bf4190d0c6b6b03e0ab1f5359e3d0e44f8c1078f0`.

## Sanitizacao

A captura contem somente a tabela, com codigos e descricoes do catalogo de
impostos; nao foram encontrados nome de fornecedor, CNPJ, usuario ou numero de
CR em seus textos e rotulos. A copia remove os metadados de execucao `lsdata`,
`lsevents`, `data-hint` e os demais atributos internos fora da lista estrutural.
Isso elimina os enderecos do ambiente e os descritores de eventos do SAP.
Comentarios tambem sao descartados. Nao ha scripts ou recursos externos na
fixture; as referencias SVG restantes apontam apenas para fragmentos locais.

Foram preservados todos os elementos, sua ordem e aninhamento, textos e entidades,
IDs de controles `WD...`, classes, estilos inline, atributos `role`/`aria-*`,
rotulos e atributos de layout. IDs de controles sao identificadores do DOM, sem
dados do fornecedor ou da sessao. Atributos preservados alem de `aria-*`:
`id`, `role`, `class`, `style`, `title`, `align`, `valign`, `width`, `height`,
`cellpadding`, `cellspacing`, `border`, `colspan`, `rowspan`, `tabindex`,
`preserveAspectRatio`, `viewBox`, `focusable`, `draggable` e `xlink:href` local.
Quebras de linha apos `</tr>` facilitam a revisao; a estrutura de linhas e
celulas permanece a da captura, inclusive as tabelas aninhadas de acoes.

## Contrato exercitado

O caso **2b** de `codigo/testar_parser_offline.py` carrega a fixture em Edge
headless local, com o CSS basico de layout da suite, e chama os helpers reais
`ler_linhas_impostos(strict_rowcount=True, com_meta=True, critico=True)` e
`ler_tabela_impostos_integral()`.

Exige 64 linhas, o multiconjunto exato de `TAX_CODES`, todas as descricoes
preenchidas, uma unica tabela candidata, `aria-rowcount=65` e leitura das
colunas pelo cabecalho (Type no indice 2, Description no 3). Nao faz login,
nao acessa o SAP e nao testa persistencia ou o conteudo atual do servidor.
