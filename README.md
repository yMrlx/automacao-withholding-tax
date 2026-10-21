# Automação de Withholding Tax — SAP MDG (Web Dynpro)

> Automação em Python + Selenium (Edge) que insere os códigos de Withholding Tax em
> Change Requests de criação de fornecedor no SAP MDG, confere cada linha, salva e envia
> a CR para aprovação. Inclui uma suíte offline (`codigo/testar_parser_offline.py`) que
> roda contra DOMs sintéticos no Edge headless, sem acesso ao SAP.
>
> Esta é uma cópia pública: endereços, números de CR, nomes e dados de fornecedores
> foram trocados por exemplos, e as notas internas de desenvolvimento não estão aqui.
> Para usar, ajuste as URLs em `codigo/config.py`. O texto abaixo é o manual de uso.

Este programa insere os 64 códigos de Withholding Tax nas Change Requests (CRs) de
**criação de fornecedor**, salva e envia cada CR para aprovação, tudo numa execução só.
Você cola a lista de CRs, confirma uma vez, faz o login e acompanha as mensagens.

## Antes da primeira execução

- **Python 3** precisa estar instalado. Se não estiver, o `Rodar automacao.bat` avisa e
  mostra o link. Na primeira execução ele instala sozinho o que falta (precisa de
  internet).
- O tile **MDG Display Change Requests** precisa estar na sua tela inicial do SAP.
- As CRs já precisam existir, em rascunho, ser de criação e ter a tabela de Withholding
  Tax **vazia**. O programa não cria CR nem continua uma tabela preenchida pela metade.

## Como executar

1. Dê dois cliques em **`Rodar automacao.bat`** e aperte uma tecla no aviso inicial.
2. Cole os números das CRs (um por linha ou separados por vírgula) e aperte **ENTER
   duas vezes**. Se alguma entrada não for um número, o programa recusa a lista
   inteira: confira e cole de novo. Números repetidos são ignorados.
3. Confira a lista e a previsão de tempo que aparecem na tela.
4. Digite a frase pedida e aperte ENTER. No pacote de produção ela é
   **`PRODUCAO SALVAR E SUBMETER`** (maiúsculas e minúsculas tanto faz). Essa é a
   **única** confirmação: depois dela o programa não pergunta mais nada.
5. Faça o login no Edge que abrir. Se a tela de login pedir o idioma, escolha
   **EN - English**: em português o programa não reconhece a tela.
6. Depois do login, **não clique em nada no SAP**. Pode minimizar o Edge e usar o
   computador normalmente. A janela pode reaparecer sozinha, uma vez por CR; é normal.
7. No fim, leia o resumo e aperte ENTER para fechar o navegador.

**Uma execução por vez.** Não abra dois `Rodar automacao.bat` ao mesmo tempo.

## O que o programa faz em cada CR

1. Abre a CR e confere que ela não está travada e que o company code é o **X401**.
2. Confere que a tabela de impostos está vazia. Se houver dados, para.
3. Insere os 64 códigos, conferindo cada um na tabela.
4. Relê a tabela inteira e abre o detalhe do primeiro e do último código.
5. Salva e confere que o SAP confirmou o Save.
6. Fecha e reabre a CR e clica **Submit uma única vez**. Só considera enviada quando o
   SAP mostra "You have submitted change request…".

Se qualquer conferência falhar, ele **para** antes de seguir para a próxima CR. Ele
nunca clica Submit duas vezes.

## Quando o SAP está lento

O programa espera o SAP terminar cada etapa antes de seguir. Se uma etapa demorar, a
tela mostra a cada 10 segundos uma mensagem como esta:

```text
[90000006] W8: SAP ainda processando o Done - aguardando ha 30 s, no maximo 180 s (SAP ocupado).
```

Ela quer dizer que o programa **continua trabalhando**: aguarde e não clique em nada. O
nome da etapa pode ser **New**, **campo Type**, **campo Code**, **preenchimento** ou
**Done**. Cada etapa espera no máximo 3 minutos. Se não terminar nesse prazo, ou se a
tela mostrar um sinal de que o SAP ficou indisponível (aviso de desligamento, volta para
o login, erro), o programa para sem salvar e diz o motivo.

## Tempo

Num dia normal, cerca de **16 minutos por CR**, mais o login. Se o SAP estiver lento,
leva mais: o programa espera em vez de falhar.

A tela mostra uma previsão antes de você confirmar e a recalcula durante a execução,
com o ritmo real daquela CR ("faltam cerca de X min para os codigos desta CR") e, num
lote, das CRs já concluídas. Quando os 64 códigos terminam, a CR **ainda não acabou**:
seguem a conferência final, o Save e o Submit. Espere o resumo final.

## Como ler o resultado

No fim aparece uma linha assim:

```
Concluido. OK: 3 | Com ressalva: 0 | Falha: 0
```

- **OK** — CR inserida, salva e **enviada para aprovação**. Nada a fazer.
- **Com ressalva** — o programa **já clicou Save ou Submit** nessa CR, mas não conseguiu
  provar o resultado. O lote para ali.
  **Não rode essa CR de novo.** Abra-a no SAP e veja como ela ficou (os 64 códigos estão
  lá? ela foi enviada?). Mande o log e os prints para quem cuida da automação.
- **Falha** — o programa parou **antes** de salvar: nada foi gravado. A CR pode ficar
  **travada** no SAP por algumas horas (às vezes até um dia) e só o próprio SAP libera.
  Tentar de novo na hora costuma falhar por causa da trava: tente mais tarde ou no dia
  seguinte. Se a mensagem disser **"Parou por sinal de indisponibilidade (...)"**, o
  programa viu um sinal de problema no SAP ou na janela; o motivo está entre parênteses
  e no log.
- **CRs não processadas** — o lote parou antes de chegar nelas. Estão intactas e podem
  ir numa próxima execução.

**Depois de uma falha, não abra os Details da CR só para olhar a tabela:** isso a trava de
novo. Na próxima execução o próprio programa confere se a tabela está vazia antes de
começar.

O `Rodar automacao.bat` também resume no final: **[CONFERIR]** quer dizer "com ressalva,
não repita"; **[ATENCAO]** quer dizer falha ou CRs que ficaram para trás.

## Se algo der errado

Guarde o arquivo mais recente da pasta **`logs`** e as imagens da pasta **`prints`** e
mande para quem cuida da automação, com o número da CR. Se o SAP mostrou alguma mensagem
na tela (de encerramento, de sessão expirada), anote o texto e o horário e mande junto.
Não apague essas pastas antes disso.

O programa nunca digita nem guarda sua senha: o login é sempre feito por você.
