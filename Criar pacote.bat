@echo off
setlocal enableextensions
rem Sem chcp 65001 aqui, de proposito: com ele o set /p abaixo le VAZIO quando
rem a entrada vem redirecionada, e PRODUCAO virava um pacote de pre-producao
rem (medido em 06/10). Este .bat so mostra texto sem acento.
title Criar pacote

rem ============================================================
rem  Monta a pasta/zip pra enviar pra usuaria (saida: entrega\,
rem  git-ignorada). Quem faz o trabalho e' codigo\gerar_pacote.py:
rem  empacota so o que esta COMMITADO e liga o fluxo unico (inserir,
rem  salvar, enviar) so na copia. Rode de novo a cada mudanca.
rem ============================================================

pushd "%~dp0"
rem erro ate o gerador rodar e devolver o proprio codigo (Codex, 62a)
set "SAIDA=1"

set "PY="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python --version >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ERRO] Python 3 nao encontrado neste computador.
    goto :fim
)

echo Para qual ambiente e' o pacote?
echo   ENTER     = pre-producao ^(teste^)
echo   PRODUCAO  = producao ^(uso real^)
set "AMB="
set /p "AMB=> "
echo.

%PY% codigo\gerar_pacote.py "%AMB%"
set "SAIDA=%errorlevel%"

:fim
echo.
popd
pause
endlocal & exit /b %SAIDA%
