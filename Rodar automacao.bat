@echo off
setlocal enableextensions
rem Erro ate o Python rodar: as saidas antecipadas (sem Python, sem
rem dependencias, pasta errada) nao podem devolver 0 (QA de 06/10).
set "SAIDA=1"
chcp 65001 >nul 2>&1
title Automacao de Withholding Tax - SAP iShift / MDG

pushd "%~dp0"

echo ============================================================
echo   Automacao de Withholding Tax - SAP iShift / MDG
echo ============================================================
echo.

if not exist "codigo\sap_ishift_tax_automation.py" (
    echo [ERRO] Nao encontrei "codigo\sap_ishift_tax_automation.py".
    echo Este arquivo .bat precisa ficar na pasta raiz do projeto,
    echo junto da pasta "codigo". Nao mova ele pra outro lugar.
    goto :fim
)

rem --- Procura o Python: primeiro "py -3", depois "python" ---
set "PY="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python --version >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ERRO] Python 3 nao encontrado neste computador.
    echo.
    echo Instale o Python 3 e rode este .bat de novo:
    echo     https://www.python.org/downloads/windows/
    echo Na tela de instalacao, marque "Add python.exe to PATH".
    echo.
    echo Sem permissao pra instalar? Fale com o suporte de TI.
    goto :fim
)


rem --- Instala as dependencias so se estiver faltando ---
%PY% -c "import selenium" >nul 2>&1
if errorlevel 1 (
    echo.
    echo Primeira execucao: instalando dependencias ^(precisa de internet^)...
    %PY% -m pip install -r "requirements.txt"
    if errorlevel 1 (
        echo.
        echo [ERRO] Falha ao instalar as dependencias.
        echo Tente manualmente, nesta pasta:
        echo     %PY% -m pip install -r requirements.txt
        goto :fim
    )
)

rem --- Modo de execucao ---
rem Sempre NORMAL. O modo passo-a-passo (pausa a cada acao) existe so pra
rem desenvolvimento e continua disponivel rodando o .py direto, sem este .bat:
rem     python codigo\sap_ishift_tax_automation.py
rem (o default do proprio script ja e' passo-a-passo). Perguntar isso aqui so
rem adicionava uma decisao que o usuario final sempre responde igual.
set "MDG_SEM_PAUSA=1"

echo.
echo ------------------------------------------------------------
echo   LEMBRETES:
echo   - O login no SAP e feito por VOCE, na janela do Edge que abrir.
echo   - Escolha o idioma "EN - English" na tela de login (nao Portugues).
echo ------------------------------------------------------------
echo.
pause

cd codigo
%PY% sap_ishift_tax_automation.py
set "SAIDA=%errorlevel%"
cd ..

echo.
rem Codigo 3 nao e' erro: uma acao de persistencia (Save ou Submit) JA FOI
rem clicada, mas o resultado precisa de conferencia. Tratar como falha faria a
rem pessoa repetir - e repetir e' justamente o errado.
if "%SAIDA%"=="3" (
    echo [CONFERIR] O programa executou uma acao, mas o resultado tem ressalva.
    echo NAO rode de novo essas CRs. Abra cada uma no SAP e confira o estado real
    echo e os dados; depois avise quem cuida da automacao, com o arquivo da pasta
    echo "logs" e a imagem da pasta "prints".
    echo As CRs que o resumo lista como NAO processadas nao foram tocadas e podem
    echo ir numa proxima execucao.
) else if not "%SAIDA%"=="0" (
    echo [ATENCAO] O programa terminou com codigo de erro %SAIDA%.
    echo Veja as mensagens acima e os arquivos nas pastas "logs" e "prints".
) else (
    echo Programa finalizado. Log salvo em "logs\", prints em "prints\".
)

:fim
echo.
popd
pause
rem Propaga o codigo de saida do Python. Sem isto o .bat devolve 0
rem mesmo quando a automacao falhou, e quem chama nao percebe.
endlocal & exit /b %SAIDA%
