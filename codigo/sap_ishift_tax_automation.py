"""
Automacao de insercao de Withholding Tax Types no SAP iShift (MDG).

Fluxo unico implementado (INSERIR_SALVAR_SUBMETER), com navegacao por
"Details" de LINHA DE TABELA nos niveis intermediarios:
  Display Change Requests -> buscar CR pelo numero -> abrir
  -> secao "ERP Vendors" (tabela) -> "Details" na linha -> tela "ERP Vendor"
  -> secao "Company Codes" (tabela) -> "Details" na linha
     -> tela "ERP Vendor Company Code"
  -> secao "ERP Vendor Company Code: Extended Withholding Tax Types"
  -> para cada um dos 64 codigos de imposto (Pessoa Juridica):
       New -> Withholding Tax Type -> Withholding Tax Code -> flegar
       "Subject to Withholding Tax" -> Done
  -> conferir os Details conforme DETAILS_NO_FLUXO_UNICO e reler a tabela
     integral: exatamente os 64 Types, sem duplicados ou Description vazia
  -> Done -> Done -> Save CONFIRMADO
  -> fechar a janela da CR e reabri-la no mesmo navegador
  -> um unico Submit -> verificar os sinais positivos de envio.

Validacao em pre-producao, registrada em CLAUDE.md:
  - navegacao real confirmada em 27/08/2026;
  - insercao das 64 linhas e persistencia do Save confirmadas em 23/09/2026;
  - sessao B (conferencia completa -> Save -> reabrir -> Submit) e fluxo
    unico completos em 29/09/2026. O fluxo unico na CR 90000001 conferiu
    TODOS os 64 Details e terminou com Save e Submit confirmados.
Essa prova usou os tempos conservadores e conferencia completa. Conferencia
por amostra (primeiro e ultimo Details), tempos otimizados e lote com varias
CRs dependem da evidencia das suas proprias rodadas de validacao.

O atestado prende a prova de insercao a esta CR e session_id. Save com
ressalva ou efeito incerto interrompe o fluxo; Submit nunca e' repetido.
A confirmacao do envio exige mensagem "You have submitted change request"
da CR correta, ausencia de Submit/Withdraw Draft, controle Workflow Log e
Save/Cancel desabilitados; o status da CR nao e' usado como prova de envio.

SALVAR, SUBMETER e INSERIR_SALVAR_SUBMETER sao modos mutuamente exclusivos;
todos False = insercao sem persistir. AMBIENTE e URL ficam em config.py.
O login e' manual e os seletores esperam a tela em ingles. STEP_CONFIRM
habilita pausas de inspecao; o lancador usa MDG_SEM_PAUSA=1 para desliga-las.
"""

import os
import re
import sys
import time
import logging
from collections import Counter
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    InvalidElementStateException,
    ElementClickInterceptedException,
    UnexpectedAlertPresentException,
    StaleElementReferenceException,
    NoSuchWindowException,
    InvalidSessionIdException,
)

from config import AMBIENTE, ISHIFT_URL

# ============================================================
# CONFIGURACAO - ajuste antes de rodar
# ============================================================

# Pastas de saida (irmas de codigo/, uma pasta acima deste arquivo).
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(PROJECT_DIR, "logs")
PRINTS_DIR = os.path.join(PROJECT_DIR, "prints")
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(PRINTS_DIR, exist_ok=True)

# Quanto tempo de tela PARADA o wait_for_wda_ready exige antes de seguir, e de
# quanto em quanto tempo ele checa. A medicao de 14/09 mostrou que o candidato
# otimizado (1,5/0,25) corta ~5 min nas 192 esperas de uma CR de 64 codigos.
# Mas as duas tentativas de 1o Save com a familia otimizada morreram no C0 por
# frestas diferentes do re-render (gotcha 17); a unica rodada 64/64 usou
# 2,5/0,5. Para separar "provar o Save" de "validar a otimizacao", a proxima
# sessao A usa o par conservador. Depois de Save+lock observados, trocar somente
# as duas atribuicoes _LOOP abaixo pelo par _OTIMIZADA e validar em separado.
ESTABILIDADE_PADRAO = 2.5
INTERVALO_PADRAO = 0.5
ESTABILIDADE_LOOP_OTIMIZADA = 1.5
INTERVALO_LOOP_OTIMIZADO = 0.25
ESTABILIDADE_LOOP = ESTABILIDADE_PADRAO
INTERVALO_LOOP = INTERVALO_PADRAO

WAIT_TIMEOUT = 20         # segundos de espera padrao por elemento
RELOAD_WAIT_TIMEOUT = 45  # espera maior logo apos um driver.get() (reload completo do shell Fiori)

# Esperas do loop de insercao (gotcha 26, 1a rodada em producao, 13/10): o Done
# passou de 20 s com o SAP ocupado o tempo todo, e a leitura desistiu com a
# tabela ainda por voltar. Cada fase (New, preenchimento, Done) tem prazo
# monotonico proprio, contado do seu inicio e que nunca reinicia; ate la,
# situacao ambigua continua esperando e so prova positiva de indisponibilidade
# para antes (_esperar_fase). Prazo esgotado interrompe: nunca vira clique.
TETO_ESPERA_SAP_S = 180
AVISO_ESPERA_S = 10       # de quanto em quanto tempo o log avisa que o SAP ainda processa
STEP_CONFIRM = True       # True = pausa apos cada acao pra voce conferir (recomendado no 1o teste)

# O lancador "Rodar automacao.bat" seta MDG_SEM_PAUSA=1 quando o usuario escolhe
# o modo normal (sem pausa a cada passo), pra nao precisar editar este arquivo.
# Default continua True (passo-a-passo) - so desliga com a variavel explicita.
if os.environ.get("MDG_SEM_PAUSA") == "1":
    STEP_CONFIRM = False

# Company code da empresa. Negocio, 2026-09-14: "Company code da empresa e X401
# (Sempre esse)". navigate_to_tax_section confere isso antes de entrar na linha:
# so contar "1 linha" pega quantidade, isto pega IDENTIDADE - impede escrever os
# 64 codigos da empresa num company code que nao e' dela. Pra desligar a checagem
# (exploracao), passar company_code_esperado=None.
COMPANY_CODE_ESPERADO = "X401"

# Lista fixa dos 64 codigos de Withholding Tax (Pessoa Juridica), enviada pelo negocio.
TAX_CODES = [
    "C0", "C3",
    "F0", "F1", "F2", "F3",
    "I0", "I1", "I2",
    "P0", "P1", "P9",
    "R0", "R1", "R2",
    "S0", "S1",
    "T0", "T1", "T2", "T3", "T4", "T5", "T6",
    "W0", "W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8", "W9",
    "X0", "X1", "X2", "X3", "X4", "X5", "X6", "X7", "X8", "X9",
    "Y0", "Y1", "Y2", "Y3", "Y4", "Y5", "Y6", "Y7", "Y8", "Y9",
    "Z0", "Z1", "Z2", "Z3", "Z4", "Z5", "Z6", "Z7", "Z8", "Z9",
]
assert len(TAX_CODES) == 64, f"Esperado 64 codigos, encontrado {len(TAX_CODES)}"


# ============================================================
# LOGGING
# ============================================================
# So escreve em arquivo quando esse script roda como principal (python
# sap_ishift_tax_automation.py). Quando outro script importa este modulo so
# pra reaproveitar as funcoes (os testar_*.py), fica so no console - senao
# cada import criava um log_impostos_*.txt novo em logs/ sem conteudo util.
LOG_FILE = None
# Dois destinos com niveis diferentes (2026-09-23): a TELA e' da usuaria, o ARQUIVO
# e' nosso. Detalhe tecnico (URL do ambiente, id de botao clicado, dump de
# estado) vai em log.debug() - nao aparece pra ela, mas fica registrado pra
# quando algo quebrar. Nada de informacao e' descartado; so muda de lugar.
_console = logging.StreamHandler(sys.stdout)
_console.setLevel(logging.INFO)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[_console],
)
log = logging.getLogger(__name__)


def _ativar_log_em_arquivo():
    global LOG_FILE
    LOG_FILE = os.path.join(LOGS_DIR, f"log_impostos_{datetime.now():%Y%m%d_%H%M%S}.txt")
    arquivo = logging.FileHandler(LOG_FILE, encoding="utf-8")
    arquivo.setLevel(logging.DEBUG)     # o arquivo guarda TUDO, inclusive debug
    # Sem isto o arquivo saia sem hora nem nivel (o formato do basicConfig so
    # vale pro console) - e em 28/09 nao deu pra medir o tempo por codigo por ele.
    arquivo.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logging.getLogger().addHandler(arquivo)


_AVISOU_SEM_TTY = False


def pause(msg):
    """
    Pausa manual entre acoes, so quando STEP_CONFIRM = True.

    Tolera nao haver terminal interativo (2026-09-14): antes, um EOFError aqui
    DERRUBAVA a automacao no meio do fluxo - e uma pausa de conferencia nunca
    deveria ser capaz de abortar uma rodada. Sem TTY, segue em frente avisando
    uma vez, que na pratica equivale ao modo normal (MDG_SEM_PAUSA=1).
    """
    global _AVISOU_SEM_TTY
    if not STEP_CONFIRM:
        return
    try:
        input(f"    [STEP] {msg} -- confira a tela e pressione ENTER para continuar...")
    except EOFError:
        _marcar_stdin_sem_resposta()
        if not _AVISOU_SEM_TTY:
            _AVISOU_SEM_TTY = True
            log.warning(
                "STEP_CONFIRM=True mas nao ha terminal interativo - as pausas de "
                "conferencia serao PULADAS (equivale a MDG_SEM_PAUSA=1). Isso NAO "
                "afeta as confirmacoes de seguranca, que sao lidas antes de abrir "
                "o navegador.")


# BOM (U+FEFF): aparece quando a lista de CRs e' colada de um arquivo
# salvo com assinatura, ou vem por pipe do PowerShell (visto em 22/09). Sem
# tirar isto, a 1a CR da lista vira "nao e numero" e e ignorada em silencio.
BOM = chr(0xFEFF)   # literal invisivel no fonte seria pior que a constante

# Checkpoint humano ("OK") diante da tela, antes de CADA Save/Submit, POR CR.
# DESLIGADO por decisao do operador em 2026-09-24: a autorizacao e' a palavra
# digitada UMA vez no inicio, depois de ver a lista de CRs. Motivo: a usuaria
# cola 10-28 CRs pra trabalhar em outra coisa enquanto roda; um OK por CR
# esquecido na 1a segurava o lote inteiro parado. A protecao contra clique
# errado nao depende dele: a pre-condicao confere CR, modais, mensagens e
# botoes no MESMO JS que clica, e o Submit exige atestado da sessao.
# True continua valendo pra depuracao, e ai o terminal e' exigido (abaixo).
CHECKPOINT_ANTES_DE_PERSISTIR = False


# Vira True assim que QUALQUER leitura de stdin devolver EOF. E' evidencia, nao
# predicao - ver tem_terminal_interativo().
_STDIN_SEM_RESPOSTA = False


def _marcar_stdin_sem_resposta():
    global _STDIN_SEM_RESPOSTA
    _STDIN_SEM_RESPOSTA = True


def tem_terminal_interativo():
    """
    False depois que alguma leitura de stdin ja devolveu EOF.

    NAO use sys.stdin.isatty() aqui. Medido em 2026-09-22 neste mesmo ambiente
    Windows: isatty() devolve True tanto num console de verdade quanto quando
    stdin esta em EOF imediato (processo lancado por ferramenta, stdin no
    dispositivo nulo). A 1a versao deste guard usava isatty e passou batido
    justamente no cenario que ele existia pra pegar.

    Por isso a checagem e' por EVIDENCIA: so afirma que nao ha terminal depois
    de uma leitura ter falhado de verdade. Consequencia de desenho: quem
    consulta isto precisa faze-lo DEPOIS da primeira leitura (get_cr_list), nao
    antes.
    """
    return not _STDIN_SEM_RESPOSTA


def _stdin_parece_console():
    """
    True/False se der pra perguntar ao Windows se stdin e' um console de
    verdade; None se nao der pra saber.

    Usado so pra AVISAR cedo, nunca pra abortar: um falso negativo aqui (algum
    terminal alternativo que nao responda a GetConsoleMode) impediria o
    operador de salvar, que e' pior do que o problema que isso previne. Quem
    aborta e' a evidencia (tem_terminal_interativo) e, no fim, pause_critica.
    """
    try:
        import ctypes, msvcrt
        h = msvcrt.get_osfhandle(sys.stdin.fileno())
        modo = ctypes.c_uint()
        return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(h),
                                                          ctypes.byref(modo)))
    except Exception:
        return None


def _normalizar_confirmacao(texto):
    """
    Deixa a resposta comparavel: sem espacos nas pontas, sem espaco duplo no
    meio, tudo em maiusculas. "salvar submeter" e "  SALVAR   SUBMETER " passam
    a valer o mesmo que "SALVAR SUBMETER".
    """
    return " ".join(str(texto or "").split()).upper()


def _pedir_confirmacao(esperado, tentativas=3):
    """
    Pede a palavra de confirmacao, aceitando maiusculas/minusculas e espaco
    sobrando. Devolve True se confirmou, False se gastou as tentativas.

    Por que tolerante (pedido do operador, 2026-09-23): quem vai rodar isto e' a
    usuaria, e `resp != esperado` exato transformava um "salvar" minusculo em
    encerramento - **perdendo a lista de CRs ja colada**, que e' o trabalho
    manual da execucao. O atrito estava no lugar errado: punia erro de digitacao,
    nao falta de intencao.

    Por que a palavra CONTINUA existindo, e diferente por nivel de risco
    (7a revisao do Codex): digitar "SALVAR SUBMETER" e' um ato deliberado que
    nao acontece por engano. Aceitar minusculas nao enfraquece isso - ninguem
    digita "salvar submeter" sem querer. O que enfraqueceria seria usar a MESMA
    palavra pros tres niveis, ai viraria reflexo.

    Custo real pra usuaria: isto e' UMA vez por execucao, nao por CR. Ela cola as
    ~28 CRs e confirma uma vez so.
    """
    alvo = _normalizar_confirmacao(esperado)
    for n in range(1, tentativas + 1):
        resp = input(f"Confere a lista acima e digite '{esperado}' pra continuar: ")
        if _normalizar_confirmacao(resp) == alvo:
            return True
        se_restam = tentativas - n
        if se_restam:
            print(f"    Nao foi isso. Esperava {esperado!r} (maiusculas/minusculas nao "
                  f"importam). Restam {se_restam} tentativa(s).")
        else:
            print(f"    Nao foi isso, e as tentativas acabaram.")
    return False


def pause_critica(msg):
    """
    Pausa que ABORTA quando nao ha quem responda - o oposto de pause().

    Motivo (10a revisao do Codex, 2026-09-22): os logs de 14/09 mostram
    "STEP_CONFIRM=True mas nao ha terminal interativo" e TODAS as pausas
    puladas. pause() faz isso de proposito - uma pausa de conferencia nao pode
    derrubar uma rodada (gotcha do EOFError). Mas o checkpoint imediatamente
    antes do Save/Submit nao e' conferencia: e' a ultima autorizacao humana
    diante da tela de verdade, depois de todas as checagens automaticas. Se
    ninguem pode responder, a resposta certa nao e' "segue" - e' parar.

    A confirmacao digitada no inicio do main() ("SALVAR") nao substitui esta:
    aquela e' dada ANTES de ver a tela, esta e' dada olhando pra ela.
    """
    if not CHECKPOINT_ANTES_DE_PERSISTIR:
        # Autorizado pela confirmacao inicial do main() - decisao de 24/09.
        log.info(f"sem checkpoint por CR (autorizado na confirmacao inicial): {msg}")
        return
    print("")
    print(f"    {'!'*60}")
    print(f"    [CHECKPOINT] {msg}")
    print(f"    {'!'*60}")
    try:
        resp = input("    Confira a tela. Digite 'OK' para executar (qualquer outra coisa aborta): ")
    except EOFError:
        raise RuntimeError(
            "checkpoint de persistencia sem terminal interativo: ABORTANDO antes de "
            "clicar. Nao gravo dado real sem confirmacao humana diante da tela. "
            "Rode num terminal de verdade (cmd/PowerShell/'Rodar automacao.bat'), "
            "ou desligue CHECKPOINT_ANTES_DE_PERSISTIR de forma explicita e "
            "assumindo a responsabilidade.")
    # mesma tolerancia da confirmacao inicial: 'ok' minusculo e' a MESMA intencao
    # que 'OK'. O que nao pode ser tolerado e' seguir sem resposta - isso e'
    # tratado no EOFError acima, e continua abortando.
    if _normalizar_confirmacao(resp) != "OK":
        raise RuntimeError(
            f"checkpoint recusado pelo operador (respondeu {resp!r}). Nada foi clicado.")
    log.info(f"checkpoint confirmado pelo operador: {msg}")


# ============================================================
# HELPERS DE UI (SAPUI5 / Fiori)
# ============================================================
# Quantos dialogos do navegador foram aceitos em silencio (pelo
# _safe_execute_script). Revisao de 29/09: a reabertura da CR antes do Submit
# passa por leituras permissivas; se um dialogo for aceito ali, o Submit e'
# recusado (_submeter_no_lote compara o contador antes/depois).
_ALERTAS_ACEITOS = 0


def _accept_alert_if_present(driver):
    """Aceita um alert JS pendente, se houver, e retorna o texto (ou None)."""
    global _ALERTAS_ACEITOS
    try:
        alert = driver.switch_to.alert
        texto = alert.text
        alert.accept()
        _ALERTAS_ACEITOS += 1
        return texto
    except Exception:
        return None


def _safe_execute_script(driver, script, *args):
    """
    Roda execute_script tolerando o alerta esporadico do WebDynpro visto em
    2026-08-21 ("Web Dynpro ABAP Permission denied: No validation message
    from parent received. Whitelistservice: ...") que trava QUALQUER
    chamada de JS ate ser aceito. Causa exata nao confirmada (suspeita:
    handshake postMessage entre o shell Fiori e o iframe WDA que as vezes
    nao completa a tempo). Aceita o alerta e tenta a MESMA chamada de novo
    uma vez antes de deixar a excecao subir.
    """
    try:
        return driver.execute_script(script, *args)
    except UnexpectedAlertPresentException:
        texto = _accept_alert_if_present(driver)
        log.warning(f"Alerta inesperado aceito e ignorado: {texto!r}. Tentando de novo...")
        time.sleep(1)
        return driver.execute_script(script, *args)


def criar_driver_edge(headless=False):
    """
    Cria o Edge do jeito que o caminho critico PRECISA: com
    unhandledPromptBehavior="ignore".

    Motivo (gotcha 14, Codex 2026-09-22): o default do WebDriver/W3C e'
    "dismiss and notify" - o proprio driver DESCARTA o dialogo e so depois
    levanta UnexpectedAlertPresentException. Ou seja, ate aqui a promessa de
    _execute_script_critico ("o dialogo continua ABERTO na tela - resolva
    manualmente") NAO estava codificada em lugar nenhum: o dialogo ja tinha
    sido dispensado pelo driver antes de a excecao chegar no Python, e num
    confirm() "dismiss" equivale a CANCELAR - uma decisao humana tomada em
    silencio pela infraestrutura.

    Com "ignore" o dialogo FICA na tela e todo comando seguinte falha ate
    alguem resolve-lo. E' fail-closed de proposito: numa janela de Save/Submit,
    travar e' o resultado certo. _safe_execute_script continua funcionando
    normalmente (ele aceita o alerta EXPLICITAMENTE, que e' o que sempre quis
    fazer - e agora de fato faz).
    """
    opts = webdriver.EdgeOptions()
    opts.set_capability("unhandledPromptBehavior", "ignore")
    if headless:                      # so o teste offline usa isso
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
    return webdriver.Edge(options=opts)


def _execute_script_critico(driver, script, *args):
    """
    Igual a _safe_execute_script, MAS: nao aceita alerta nenhum e NAO repete o
    script. Pra usar no caminho que PERSISTE dado (Save/Submit).

    Motivo (9a revisao do Codex, P0): _safe_execute_script aceita QUALQUER
    alerta inesperado e re-executa a mesma chamada. Isso e' certo pro alerta
    benigno do WDA ("Permission denied ... Whitelistservice"), que trava todo
    JS ate ser aceito - mas e' desastroso no clique de Save/Submit: se a acao
    abrir um confirm() nativo, o wrapper CONFIRMA sozinho e CLICA DE NOVO.
    Num Submit, isso e' irreversivel duas vezes.

    Aqui o alerta NAO e' dispensado de proposito - fica na tela pro operador
    decidir, e a excecao diz o que aconteceu.

    IMPORTANTE: "fica na tela" depende do driver ter sido criado por
    criar_driver_edge() (unhandledPromptBehavior="ignore"). Com o default do
    W3C ("dismiss and notify") o dialogo ja chega aqui DISPENSADO pelo driver.
    """
    try:
        return driver.execute_script(script, *args)
    except UnexpectedAlertPresentException:
        try:
            texto = driver.switch_to.alert.text
        except Exception:
            texto = "(nao consegui ler o texto)"
        raise RuntimeError(
            f"acao de persistencia interrompida por um dialogo do navegador: {texto!r}. "
            f"NAO aceitei e NAO repeti a acao de proposito: repetir poderia clicar duas "
            f"vezes e aceitar poderia confirmar algo que precisa de decisao humana. "
            f"O dialogo continua ABERTO na tela - resolva manualmente e confira o que "
            f"foi (ou nao foi) gravado antes de rodar de novo.")


def wait_and_click(driver, by, value, timeout=WAIT_TIMEOUT):
    el = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, value)))
    try:
        el.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", el)
    return el


def find_clickables_by_text(driver, text):
    """
    Retorna os elementos clicaveis visiveis cujo texto normalizado contenha
    `text`, do mais especifico (menor texto) para o menos.

    Por que via JavaScript e nao XPath: o SAPUI5 quebra titulos em varias linhas
    e/ou em varios elementos internos (o tile "MDG Display Change Requests"
    renderiza como "MDG Display" + "Change Requests"), entao nenhum no isolado
    contem a string inteira. Aqui usamos o innerText do elemento (que junta os
    filhos e ja reflete o texto como aparece na tela), colapsando espacos e
    quebras de linha antes de comparar.
    """
    script = """
    const norm = (s) => (s || '').replace(/\\u00ad/g, '').replace(/\\s+/g, ' ').trim();
    const alvo = norm(arguments[0]).toLowerCase();
    const candidatos = [];
    for (const el of document.querySelectorAll('a, span, div, button, li, bdi, td')) {
        const txt = norm(el.innerText);
        if (!txt || !txt.toLowerCase().includes(alvo)) continue;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;   // ignora invisiveis
        // ignora texto de acessibilidade (ARIA) posicionado bem longe da tela
        // de proposito - tem largura/altura > 0 mas nao e algo clicavel de
        // verdade (ex: WebDynpro usa spans tipo ARIA_BTN_EDIT a dezenas de
        // milhares de pixels do topo, so pra leitor de tela).
        if (Math.abs(r.top) > 10000 || Math.abs(r.left) > 10000) continue;
        candidatos.push({el: el, len: txt.length, area: r.width * r.height});
    }
    // mais especifico primeiro (texto mais curto); em empate (ex: varios
    // wrappers aninhados em volta de um mesmo texto, como um tile do
    // Fiori Launchpad), prefere o elemento de menor area - clicar num
    // elemento mais interno ainda funciona (o clique borbulha ate o
    // handler no ancestral), mas clicar num wrapper externo nao aciona
    // nada porque o clique nao se propaga pra baixo, so pra cima.
    candidatos.sort((a, b) => (a.len - b.len) || (a.area - b.area));
    return candidatos.map(c => c.el);
    """
    return _safe_execute_script(driver, script, text)


def click_link_by_text(driver, text, timeout=WAIT_TIMEOUT, occurrence=1):
    """
    Clica no elemento clicavel cujo texto visivel contenha `text`, escolhendo o
    mais especifico (menor texto) primeiro. Ajuste `occurrence` se a tela tiver
    mais de um elemento com o mesmo rotulo (ex: varios botoes "Edit" visiveis ao
    mesmo tempo) - occurrence=2 pega o segundo, e assim por diante.
    """
    fim = time.time() + timeout
    ultimo_erro = None
    while time.time() < fim:
        elementos = find_clickables_by_text(driver, text)
        if len(elementos) >= occurrence:
            el = elementos[occurrence - 1]
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
                el.click()
                return el
            except Exception as e:      # elemento pode sumir/re-renderizar no meio
                ultimo_erro = e
                try:
                    driver.execute_script("arguments[0].click();", el)
                    return el
                except Exception as e2:
                    ultimo_erro = e2
        time.sleep(0.5)

    raise TimeoutException(
        f"Nao encontrei elemento clicavel com texto '{text}' "
        f"(ocorrencia {occurrence}) apos {timeout}s. Ultimo erro: {ultimo_erro}"
    )


def _bounding_info(driver, el):
    return _safe_execute_script(
        driver,
        "const el = arguments[0]; const r = el.getBoundingClientRect();"
        "return {top: r.top, left: r.left, width: r.width, height: r.height};",
        el,
    )


def scroll_to_text(driver, text, timeout=WAIT_TIMEOUT, tamanho_max=(400, 100)):
    """
    Rola a tela ate o primeiro elemento com `text` que pareca um TITULO DE
    SECAO de verdade (nao um wrapper/container gigante que so por acaso
    contem o texto em algum lugar dentro dele e cuja bounding box comeca
    em Y=0 por estar cortado no topo do viewport). `tamanho_max` e mais
    largo que um botao comum porque titulo de secao e texto maior. Nao
    clica em nada, so posiciona a tela - usar antes de procurar acoes
    (New/Details/Edit) daquela secao especifica com `click_visible_candidate`.
    """
    fim = time.time() + timeout
    w_max, h_max = tamanho_max
    ultimo_erro = None
    while time.time() < fim:
        try:
            candidatos = find_clickables_by_text(driver, text)
            if candidatos:
                infos = [_bounding_info(driver, el) for el in candidatos]
                candidatos_ok = [
                    (el, info) for el, info in zip(candidatos, infos)
                    if 0 < info["width"] <= w_max and 0 < info["height"] <= h_max
                ]
                if candidatos_ok:
                    el, _ = candidatos_ok[0]
                    driver.execute_script("arguments[0].scrollIntoView({block: 'start'});", el)
                    return el
        except StaleElementReferenceException as e:
            # Nunca reaproveita a lista antiga: se UM candidato morreu, o WDA
            # pode ter trocado a secao inteira. Reacha tudo na proxima volta.
            ultimo_erro = e
        time.sleep(0.5)
    raise TimeoutException(
        f"Nao achei secao/titulo com texto '{text}' apos {timeout}s. "
        f"Ultimo erro: {ultimo_erro}")


def click_visible_candidate(driver, text, timeout=WAIT_TIMEOUT, tamanho_max=(100, 60)):
    """
    Clica no candidato com texto `text` que esteja de fato VISIVEL na tela
    (topo >= -5px, considerando pequena tolerancia) e com tamanho
    compativel com um link/botao real - nao um wrapper/container gigante
    cuja bounding box comeca em Y=0 por estar cortado no topo do viewport.

    Existe porque telas com varias tabelas na mesma pagina (ex: "ERP
    Vendors" e "Company Codes", cada uma com sua propria acao "Details" de
    linha) tem VARIOS elementos reais com o mesmo texto - o de uma secao
    ja rolada pra fora da tela ainda bate no seletor por texto e, sem esse
    filtro, podia ser escolhido no lugar do da secao certa (mesma familia
    do gotcha do "Edit" repetido - ver CLAUDE.md). Chamar `scroll_to_text`
    pra secao certa ANTES de chamar isto.
    """
    fim = time.time() + timeout
    w_max, h_max = tamanho_max
    ultimo_erro = None
    while time.time() < fim:
        try:
            candidatos = find_clickables_by_text(driver, text)
            if candidatos:
                infos = [_bounding_info(driver, el) for el in candidatos]
                visiveis = [
                    (el, info) for el, info in zip(candidatos, infos)
                    if info["top"] >= -5
                    and 0 < info["width"] <= w_max
                    and 0 < info["height"] <= h_max
                ]
                if visiveis:
                    visiveis.sort(key=lambda par: par[1]["top"])
                    el, _ = visiveis[0]
                    try:
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});", el)
                        el.click()
                        return el
                    except StaleElementReferenceException:
                        raise
                    except Exception as e:
                        ultimo_erro = e
                        try:
                            driver.execute_script("arguments[0].click();", el)
                            return el
                        except StaleElementReferenceException:
                            raise
                        except Exception as e2:
                            ultimo_erro = e2
        except StaleElementReferenceException as e:
            # Stale na medicao, no scroll ou no clique: a unica recuperacao
            # segura e' descartar TODAS as refs e recomecar a selecao.
            ultimo_erro = e
        time.sleep(0.5)
    raise TimeoutException(
        f"Nao encontrei candidato VISIVEL e com tamanho de botao pra '{text}' "
        f"apos {timeout}s. Ultimo erro: {ultimo_erro}"
    )


def find_input_by_label(driver, label_text, input_type=None):
    """
    Retorna o input associado a um label, casando o texto do label de forma
    normalizada (mesma razao do find_clickables_by_text: SAPUI5 quebra e aninha
    textos). Procura o proximo input na ordem do documento apos o label.

    input_type: se informado (ex: 'checkbox'), so considera inputs desse tipo.
    """
    return _safe_execute_script(driver, _JS_ACHAR_INPUT_POR_LABEL, label_text, input_type)


# Corpo do find_input_by_label, reusado por _JS_CAMPO_POR_LABEL (mesma busca).
_JS_ACHAR_INPUT_POR_LABEL = """
    const norm = (s) => (s || '').replace(/\\u00ad/g, '').replace(/\\s+/g, ' ').trim();
    const alvo = norm(arguments[0]).toLowerCase();
    const tipo = arguments[1];

    // 1) acha o label cujo texto normalizado contenha o alvo (o mais curto)
    let label = null, menor = Infinity;
    for (const el of document.querySelectorAll('label, span, div, td, bdi')) {
        const txt = norm(el.innerText);
        if (!txt || !txt.toLowerCase().includes(alvo)) continue;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        if (Math.abs(r.top) > 10000 || Math.abs(r.left) > 10000) continue;  // texto de ARIA fora da tela
        if (txt.length < menor) { menor = txt.length; label = el; }
    }
    if (!label) return null;

    // 2) se o label aponta pra um input via 'for', usa ele direto
    const forId = label.getAttribute && label.getAttribute('for');
    if (forId) {
        const alvoDireto = document.getElementById(forId);
        if (alvoDireto && (!tipo || alvoDireto.type === tipo)) return alvoDireto;
    }

    // 3) senao, pega o proximo input visivel na ordem do documento
    const inputs = Array.from(document.querySelectorAll('input'));
    const todos = Array.from(document.querySelectorAll('*'));
    const posLabel = todos.indexOf(label);
    for (const inp of inputs) {
        if (todos.indexOf(inp) <= posLabel) continue;
        if (tipo && inp.type !== tipo) continue;
        const r = inp.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        return inp;
    }
    return null;
"""

# Estado do input de um label, numa chamada so (sem WebElement entre passos,
# gotcha 17): achou, aceita digitacao, valor. Usado pelas condicoes das fases.
_JS_CAMPO_POR_LABEL = (
    "const inp = (function () {\n" + _JS_ACHAR_INPUT_POR_LABEL
    + "\n}).apply(null, arguments);\n" + """
    if (!inp) return {achou: false};
    const editavel = !(inp.disabled || inp.readOnly
        || (inp.getAttribute('aria-readonly') || '') === 'true'
        || (inp.getAttribute('aria-disabled') || '') === 'true');
    return {achou: true, editavel: editavel, valor: inp.value || ''};
""")


def _wait_for_input(driver, label_text, timeout, input_type=None):
    fim = time.time() + timeout
    while time.time() < fim:
        campo = find_input_by_label(driver, label_text, input_type)
        if campo is not None:
            return campo
        time.sleep(0.5)
    raise TimeoutException(
        f"Nao encontrei campo com label '{label_text}'"
        f"{f' (tipo {input_type})' if input_type else ''} apos {timeout}s."
    )


class CampoSumiuDoDom(NoSuchElementException):
    """O id do campo saiu do DOM e nao voltou: o WDA redesenhou a sub-tela com
    id NOVO (gotcha 21). Subclasse de NoSuchElementException de proposito: o
    fill_field_by_label ja trata essa excecao como "re-acha pelo label"."""


def _esperar_campo_editavel(driver, campo_id, timeout=10, sumico_max=1.5):
    """
    Espera um input ficar REALMENTE editavel. Trabalha pelo **id**, nunca por
    referencia de elemento.

    Existir no DOM nao basta: numa sub-tela WDA recem-aberta o input ja aparece
    e ja e' encontrado pelo find, mas ainda chega desabilitado/readonly por um
    instante. Foi o que derrubou a rodada de 2026-09-14 15:36 -
    InvalidElementStateException no campo.clear(), 1,8s depois de abrir o "New".

    Esperar a TELA estabilizar pra inferir que o CAMPO esta pronto e' impreciso
    nas duas direcoes: espera demais quando ele ja aceita digitacao, e de menos
    quando ele demora mais que o resto. Aqui a espera e' do proprio campo.

    POR QUE POR ID e nao pelo elemento (gotcha 17, rodada de 2026-09-22 14:08):
    a versao anterior recebia o WebElement achado pelo find e o repassava pro
    execute_script. O WDA RE-RENDERIZA a sub-tela logo depois de abri-la, e ai
    o no' que o find devolveu deixa de existir - o WebDriver recusa a chamada
    com StaleElementReferenceException ANTES de o script rodar. A checagem
    `el.isConnected` que existia aqui pra esse caso nunca chegava a executar.
    Buscando por getElementById DENTRO do JS, o re-render vira apenas mais uma
    iteracao do polling, que e' o que ele sempre foi.

    GOTCHA 21 (2026-09-29 10:01, 1a rodada com 1,5/0,25): o WDA tambem pode
    redesenhar a sub-tela com id NOVO. O id antigo some e nao volta, e esperar
    por ele ate o timeout derrubou a rodada no C0 com a sub-tela pronta no
    print. Se o id ficar ausente por `sumico_max` segundos seguidos, levanta
    CampoSumiuDoDom e quem chamou re-acha o campo pelo label. O re-render com o
    MESMO id (gotcha 17) deixa o id ausente por milissegundos, bem abaixo disso.
    """
    fim = time.time() + timeout
    ultimo = None
    ausente_desde = None
    while time.time() < fim:
        estado = _safe_execute_script(driver, """
            const el = document.getElementById(arguments[0]);
            if (!el) return {ok: false, ausente: true, motivo: 'ainda nao esta no DOM (re-render do WDA)'};
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return {ok: false, motivo: 'invisivel'};
            if (el.disabled) return {ok: false, motivo: 'disabled'};
            if (el.readOnly) return {ok: false, motivo: 'readonly'};
            if ((el.getAttribute('aria-readonly') || '') === 'true') return {ok: false, motivo: 'aria-readonly'};
            if ((el.getAttribute('aria-disabled') || '') === 'true') return {ok: false, motivo: 'aria-disabled'};
            return {ok: true};
        """, campo_id)
        if isinstance(estado, dict) and estado.get("ok"):
            return True
        if isinstance(estado, dict) and estado.get("ausente"):
            ausente_desde = ausente_desde or time.time()
            if time.time() - ausente_desde >= sumico_max:
                raise CampoSumiuDoDom(
                    f"campo id={campo_id!r} saiu do DOM e nao voltou em {sumico_max}s: o WDA "
                    f"redesenhou com id novo (gotcha 21) - re-achar pelo label.")
        else:
            ausente_desde = None
        ultimo = (estado or {}).get("motivo", estado)
        time.sleep(0.2)
    raise TimeoutException(
        f"campo id={campo_id!r} nao ficou editavel em {timeout}s (ultimo estado: {ultimo!r}). "
        f"Numa CR TRAVADA o campo aparece mas nunca fica editavel - conferir o lock.")


def fill_field_by_label(driver, label_text, value, timeout=WAIT_TIMEOUT):
    """
    Preenche o input do label e dispara o onchange do SAPUI5 (TAB).

    Tolera o re-render do WDA (gotcha 17): a referencia de elemento devolvida
    pelo find morre se o SAP re-renderizar a sub-tela no meio do caminho. A
    troca pode aparecer como StaleElementReferenceException, como ausencia
    momentanea entre a espera e o re-find (NoSuchElementException), ou como um
    novo no' que voltou readonly (InvalidElementStateException). Aqui esses tres
    casos sao motivo pra re-achar e tentar de novo, dentro do mesmo timeout.
    Repetir e' seguro porque clear()+send_keys() deixa o campo no mesmo estado
    final, independente de quantas vezes rodar.
    """
    fim = time.time() + timeout
    ultimo = None
    transitorias = (
        StaleElementReferenceException,
        NoSuchElementException,
        InvalidElementStateException,
    )
    while time.time() < fim:
        restante = max(0.2, fim - time.time())
        campo = _wait_for_input(driver, label_text, restante)
        try:
            campo_id = _safe_execute_script(driver, "return arguments[0].id || '';", campo) or ""
            _safe_execute_script(driver, "arguments[0].scrollIntoView({block: 'center'});", campo)
            if campo_id:
                _esperar_campo_editavel(
                    driver, campo_id, timeout=min(10, max(0.2, fim - time.time())))
                # re-obtem FRESCO: entre a espera e a digitacao o no' pode ter
                # sido trocado de novo, e o find por id e' barato.
                campo = driver.find_element(By.ID, campo_id)
            campo.clear()
            campo.send_keys(value)
            campo.send_keys(Keys.TAB)  # dispara validacao/onchange do SAPUI5
            return campo
        except transitorias as e:
            ultimo = e
            if time.time() >= fim:
                break
            log.info(
                f"campo '{label_text}' mudou durante o preenchimento "
                f"({type(e).__name__}) - re-achando e tentando de novo.")
            time.sleep(0.3)
    raise TimeoutException(
        f"campo '{label_text}': o WDA trocou/bloqueou o elemento em todas as "
        f"tentativas durante {timeout}s; nao consegui preencher. "
        f"ultimo={type(ultimo).__name__ if ultimo else None}: {ultimo}")


def switch_to_new_window(driver, janelas_antes, timeout=WAIT_TIMEOUT):
    """
    Espera uma janela/aba nova abrir (diferente das que ja existiam em
    `janelas_antes`) e troca o foco do Selenium pra ela. Alguns links do
    WebDynpro (ex: abrir uma CR a partir da lista de busca) abrem em
    aba/janela nova em vez de navegar na mesma.
    """
    fim = time.time() + timeout
    novas = []
    while time.time() < fim:
        novas = [j for j in driver.window_handles if j not in janelas_antes]
        if novas:
            break
        time.sleep(0.5)
    if not novas:
        raise TimeoutException(f"Nenhuma janela/aba nova abriu apos {timeout}s.")
    driver.switch_to.window(novas[-1])
    return novas[-1]


# O overlay de "tela ocupada" do WDA so bloqueia com tamanho real: depois de
# carregar ele fica "estacionado" em 1x1 px (gotcha 9). Mesma regra para
# wait_for_wda_ready e aguardar_resultado_done.
_JS_BLINDLAYER_BLOQUEANDO = """
    const el = document.querySelector('[id*="blindlayer"]');
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 2 || r.height <= 2) return false;  // "estacionado", nao bloqueia de verdade
    const cs = window.getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity || '1') > 0;
"""


def wait_for_wda_ready(driver, timeout=RELOAD_WAIT_TIMEOUT,
                       estabilidade=None, intervalo=None, critico=False):
    """
    Espera uma tela WebDynpro ABAP terminar de carregar. O WDA usa um
    overlay de "tela ocupada" (elemento com id contendo 'blindlayer')
    enquanto carrega/processa - em vez de um sleep fixo (que falhou: a tela
    as vezes ainda esta so parcialmente renderizada quando o texto alvo ja
    aparece no DOM), espera esse overlay sumir E o texto do body parar de
    variar por alguns segundos seguidos.

    Diagnosticado em 2026-08-26 (log instrumentado por iteracao) por que
    isso sempre estourava os 45s de timeout nessa tela mesmo com a tela
    pronta bem antes: o texto do body ESTABILIZAVA rapido (~15-18s), mas o
    elemento 'blindlayer' NUNCA e removido nem escondido de verdade depois
    que a tela termina de carregar - ele so encolhe pra 1x1 pixel (visto:
    de 370x50 pra 1x1, mantendo display:block/visibility:visible/opacity
    baixa) e fica "estacionado" no DOM pelo resto da sessao. `querySelector`
    e ate uma checagem simples de `display`/`visibility` continuavam
    achando ele "visivel", entao a contagem de estabilidade nunca fechava.
    Corrigido: só conta como bloqueando se tiver tamanho de verdade na tela
    (>2px de largura/altura) - o estado "estacionado" (1x1) e tratado como
    inofensivo.

    critico=True (gotcha 14, achado pelo Codex em 2026-09-22): usa
    _execute_script_critico nas leituras do polling, em vez do wrapper que
    ACEITA alerta. Obrigatorio na janela de persistencia: sem isso, um
    alert/confirm que o WDA abre DEPOIS que o JS do clique ja retornou seria
    aceito por esta funcao em silencio - justamente o dialogo que o caminho
    critico promete deixar pro operador. Fora dessa janela o default (False)
    continua certo: o alerta benigno do WDA ("Permission denied ...
    Whitelistservice") trava todo JS ate ser aceito.
    """
    TOLERANCIA_CHARS = 15  # variacao aceita entre checagens sem resetar a contagem de estabilidade
    # estabilidade/intervalo parametrizados em 2026-09-14. O default (2,5s / 0,5s)
    # foi calibrado em 26/08, quando esta funcao estava quebrada pelo blindlayer
    # "estacionado" (gotcha 9), e nunca foi revisto depois do conserto. Ele serve
    # pra abrir uma CR inteira. O candidato otimizado do loop corta ~5 min numa
    # CR, mas esta temporariamente desacoplado do 1o Save; ver constantes no topo
    # e a medicao no CLAUDE.md.
    estabilidade = ESTABILIDADE_PADRAO if estabilidade is None else estabilidade
    intervalo = INTERVALO_PADRAO if intervalo is None else intervalo
    exec_js = _execute_script_critico if critico else _safe_execute_script
    fim = time.time() + timeout
    tamanho_anterior = -1
    estavel_desde = None
    while time.time() < fim:
        tem_blindlayer_visivel = exec_js(driver, _JS_BLINDLAYER_BLOQUEANDO)
        tamanho_atual = exec_js(driver, "return (document.body.innerText || '').length;")

        variacao_pequena = abs(tamanho_atual - tamanho_anterior) <= TOLERANCIA_CHARS
        if not tem_blindlayer_visivel and tamanho_atual > 0 and variacao_pequena:
            if estavel_desde is None:
                estavel_desde = time.time()
            elif time.time() - estavel_desde >= estabilidade:
                return
        else:
            estavel_desde = None

        tamanho_anterior = tamanho_atual
        time.sleep(intervalo)

    raise TimeoutException(f"Tela WebDynpro nao estabilizou apos {timeout}s.")


# Acha o checkbox de um label pela POSICAO, nao pelo texto do container.
# Confirmado no dump/screenshot de 2026-09-09 (tela "Extended Withholding Tax
# Type"): o checkbox "Subject to Withholding Tax" e um <span role="checkbox">
# numa celula SEM texto nenhum, ao lado da celula do label. Casar pelo
# innerText do container do checkbox (abordagem antiga) nunca casava.
# Estrategia: acha o label (span/td mais "justo" com o texto), pega o
# [role=checkbox]/input[type=checkbox] visivel na MESMA linha visual (centro
# vertical a <=18px do centro do label) e mais proximo na horizontal.
_CHECKBOX_PERTO_DO_LABEL_JS = r"""
const norm = (s) => (s || '').replace(/\u00ad/g, '').replace(/\s+/g, ' ').trim().toLowerCase();
const alvo = norm(arguments[0]);

let label = null, menor = Infinity, menorArea = Infinity;
for (const el of document.querySelectorAll('label, span, div, td, bdi')) {
    const txt = norm(el.innerText);
    if (!txt || !txt.includes(alvo)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (Math.abs(r.top) > 10000 || Math.abs(r.left) > 10000) continue;
    const area = r.width * r.height;
    if (txt.length < menor || (txt.length === menor && area < menorArea)) {
        menor = txt.length;
        menorArea = area;
        label = el;
    }
}
if (!label) return null;
const lr = label.getBoundingClientRect();
const lcy = lr.top + lr.height / 2;
const lcx = lr.left + lr.width / 2;

let best = null, bestDist = Infinity;
for (const el of document.querySelectorAll('[role="checkbox"], input[type="checkbox"]')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (Math.abs((r.top + r.height / 2) - lcy) > 18) continue;
    const dist = Math.abs((r.left + r.width / 2) - lcx);
    if (dist > 400) continue;
    if (dist < bestDist) { bestDist = dist; best = el; }
}
return best;
"""

# Mesma busca posicional, mas devolve somente o ESTADO no proprio comando JS.
# Nao retorna WebElement pra o Python e portanto nao abre uma janela de stale
# entre "achar o checkbox" e "ler aria-checked" (gotcha 17 / 29a entrada).
_CHECKBOX_MARCADO_PERTO_DO_LABEL_JS = _CHECKBOX_PERTO_DO_LABEL_JS.replace(
    "return best;",
    "if (!best) return null; "
    "return best.getAttribute('aria-checked') === 'true' || best.checked === true;",
)


def _checkbox_esta_marcado(driver, el_id):
    return bool(_safe_execute_script(
        driver,
        """
        const el = document.getElementById(arguments[0]);
        if (!el) return false;
        return el.getAttribute('aria-checked') === 'true' || el.checked === true;
        """,
        el_id,
    ))


def _checkbox_marcado_perto_do_label(driver, label_text):
    """Re-acha o checkbox pela POSICAO (perto do label) e le o estado. Retorna
    True/False, ou None se nao achou. Usado pra confirmar o estado quando o
    checkbox nao tem id estavel (ref do Selenium pode ficar stale apos o
    round-trip do WDA)."""
    marcado = _safe_execute_script(
        driver, _CHECKBOX_MARCADO_PERTO_DO_LABEL_JS, label_text)
    if marcado is None:
        return None
    return bool(marcado)


def check_checkbox_by_label(driver, label_text, timeout=WAIT_TIMEOUT):
    """
    Marca o checkbox de um label e CONFIRMA (via aria-checked) que ficou
    marcado de verdade - um checkbox silenciosamente desmarcado faz o codigo
    de imposto entrar errado, e isso repetiria 64x.

    Dois formatos, testados no mesmo loop ate um aparecer:
      1) <input type=checkbox> real (via find_input_by_label) - raro nessa tela;
      2) WebDynpro ABAP classico: <span role="checkbox"> numa celula sem texto,
         ao lado do label - achado pela POSICAO (_CHECKBOX_PERTO_DO_LABEL_JS),
         nao pelo texto do container (que e vazio - dump de 2026-09-09).

    As duas checagens no mesmo loop (nao uma esperando o timeout inteiro antes
    da outra) porque insert_all_taxes() chama isto 64x.

    Depois de achar, clica e espera o aria-checked virar 'true' (WDA faz
    round-trip no servidor). Se o 1o clique nao pegar, escala: clica no pai,
    depois dispara a sequencia mousedown/mouseup/click.
    """
    fim = time.time() + timeout
    alvo_id = None
    while time.time() < fim and alvo_id is None:
        try:
            box = find_input_by_label(driver, label_text, input_type="checkbox")
            if box is None:
                box = _safe_execute_script(driver, _CHECKBOX_PERTO_DO_LABEL_JS, label_text)
            if box is not None:
                alvo_id = _safe_execute_script(driver, "return arguments[0].id;", box) or ""
                if not alvo_id:
                    # Sem id estavel: nao da pra re-achar por getElementById,
                    # mas DA pra confirmar re-achando pela posicao. Se a ref
                    # morrer antes do clique, a volta externa re-acha tudo.
                    if _checkbox_marcado_perto_do_label(driver, label_text) is True:
                        return box
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});", box)
                    driver.execute_script("arguments[0].click();", box)
                    prazo = time.time() + 4
                    while time.time() < prazo:
                        if _checkbox_marcado_perto_do_label(driver, label_text) is True:
                            return box
                        time.sleep(0.3)
                    raise TimeoutException(
                        f"checkbox '{label_text}' (sem id): cliquei mas nao confirmei "
                        f"aria-checked='true' re-achando pelo label. Fail closed."
                    )
                break
        except StaleElementReferenceException:
            alvo_id = None
        time.sleep(0.5)

    if alvo_id is None:
        raise TimeoutException(
            f"Nao encontrei checkbox para o label '{label_text}' apos {timeout}s "
            f"(nem <input type=checkbox>, nem [role=checkbox] na mesma linha do label)."
        )

    # Cada estrategia re-busca por ID, rola e clica NO MESMO JS. Assim nao ha
    # WebElement Python entre o re-find e o clique.
    estrategias = [
        "el.click();",
        "if (el.parentElement) el.parentElement.click();",
        "(function (alvo) { const o = {bubbles: true, cancelable: true, view: window};"
        " alvo.dispatchEvent(new MouseEvent('mousedown', o));"
        " alvo.dispatchEvent(new MouseEvent('mouseup', o));"
        " alvo.dispatchEvent(new MouseEvent('click', o)); })(el);",
    ]
    for acao_js in estrategias:
        if _checkbox_esta_marcado(driver, alvo_id):
            return _safe_execute_script(driver, "return document.getElementById(arguments[0]);", alvo_id)
        executou = _safe_execute_script(driver, """
            const el = document.getElementById(arguments[0]);
            if (!el) return false;
            el.scrollIntoView({block: 'center'});
        """ + acao_js + "\nreturn true;", alvo_id)
        if not executou:
            continue
        prazo = time.time() + 3
        while time.time() < prazo:
            if _checkbox_esta_marcado(driver, alvo_id):
                return _safe_execute_script(driver, "return document.getElementById(arguments[0]);", alvo_id)
            time.sleep(0.3)

    raise TimeoutException(
        f"Cliquei no checkbox '{label_text}' (id {alvo_id}) mas o aria-checked nao "
        f"virou 'true' apos {len(estrategias)} estrategias de clique."
    )


# ============================================================
# FLUXO PRINCIPAL
# ============================================================
APP_TILE_NAME = "MDG Display Change Requests"

# O app "Display Change Requests" e WebDynpro ABAP classico (USMD_EDITION_CREQUEST),
# nao SAPUI5 - o Fiori Launchpad embute ele num <iframe> de ORIGEM DIFERENTE (porta
# 44351 contra 44350 do shell), confirmado via diagnostico em 2026-08-21. Isso significa
# que nenhum document.querySelectorAll rodando no frame pai (via execute_script) alcanca
# esse conteudo - e bloqueio de cross-origin do proprio navegador, nao um seletor errado.
# O Fiori nomeia esses iframes de app como "application-<intent>", entao esse seletor
# deve seguir valendo pra qualquer app embutido assim, nao so este.
APP_IFRAME_LOCATOR = (By.CSS_SELECTOR, "iframe[id^='application-']")


ARQUIVO_FECHAR = os.path.join(PROJECT_DIR, "FECHAR_NAVEGADOR.txt")


def esperar_login_manual(driver, timeout=480, intervalo=4):
    """
    Espera o login MANUAL terminar, detectando a tela logada de verdade (o tile
    do app aparecer), em vez de confiar num ENTER do operador.

    Por que polling e nao input(): com input(), quem aperta ENTER cedo demais -
    antes de o Fiori terminar de carregar - faz o script seguir e falhar depois,
    com um erro que nao parece ter a ver com login. E, rodando sem terminal
    interativo, o input() retorna na hora e o script nem espera. Esta versao
    vinha do testar_loop_taxes.py e foi promovida pra ca (CLAUDE.md: corrigir no
    modulo, nao no teste).

    Levanta TimeoutException se o tile nao aparecer a tempo.
    """
    print(
        "\nFaca login manualmente na janela do navegador que abriu."
        "\nIMPORTANTE: se a tela de logon pedir o idioma, escolha 'EN - English' (nao"
        " 'PT - Portugues'),\nsenao os textos que o script procura nao vao bater com a tela."
        f"\n\nAguardando ate {timeout // 60} min - o programa segue SOZINHO assim que"
        " reconhecer a tela logada.\n", flush=True)
    fim = time.time() + timeout
    while time.time() < fim:
        try:
            if find_clickables_by_text(driver, APP_TILE_NAME):
                log.info("tela logada reconhecida (tile encontrado) - seguindo.")
                return True
        except Exception as e:
            raise RuntimeError(
                f"perdi a sessao do navegador enquanto esperava o login "
                f"({type(e).__name__}) - a janela foi fechada?")
        time.sleep(intervalo)
    raise TimeoutException(
        f"nao reconheci a tela logada em {timeout}s. O login foi concluido? "
        f"O idioma esta em 'EN - English', ou o tile '{APP_TILE_NAME}' nao "
        f"esta na sua tela inicial?")


def esperar_inspecao(segundos=900):
    """
    Sem terminal interativo, o input() de 'aperte ENTER pra fechar' retorna na
    hora e o navegador fecharia junto. Como cada rodada TRAVA a CR por horas,
    manter a tela aberta vale: e' a unica chance de olhar algo que nao foi
    dumpado. Sai antes se alguem criar ARQUIVO_FECHAR.
    """
    print(f"\n[inspecao] Sem terminal interativo. O navegador fica ABERTO por ate "
          f"{segundos // 60} min pra conferencia.", flush=True)
    print(f"[inspecao] Pra fechar antes, crie o arquivo: {ARQUIVO_FECHAR}", flush=True)
    fim = time.time() + segundos
    while time.time() < fim:
        if os.path.exists(ARQUIVO_FECHAR):
            try:
                os.remove(ARQUIVO_FECHAR)
            except Exception:
                pass
            print("[inspecao] sinal recebido - fechando o navegador.", flush=True)
            return
        time.sleep(5)
    print("[inspecao] tempo esgotado - fechando o navegador.", flush=True)


def go_to_display_change_requests(driver):
    """
    Abre a tela 'Display Change Requests' com 1 clique no tile favoritado na
    secao 'My Apps' da tela inicial, e troca o foco do Selenium pra dentro do
    iframe do app (WebDynpro, origem diferente - ver APP_IFRAME_LOCATOR). A
    partir daqui, ate o proximo driver.get() resetar pra 'My Home', toda
    interacao (open_cr, navigate_to_tax_section, insert_all_taxes,
    finalize_cr) acontece dentro desse frame.

    PRE-REQUISITO: a conta usada precisa ter o app "MDG Display Change Requests"
    adicionado em 'My Apps'. Se nao tiver, adicione uma unica vez pelo App Finder
    (icone de usuario -> App Finder -> buscar o app -> botao "+").
    """
    log.info("Abrindo 'Display Change Requests' pelo tile favoritado...")

    # Apos um driver.get() o shell do Fiori recarrega do zero (mais pesado que
    # navegacao interna), por isso da mais tempo nesse clique.
    click_link_by_text(driver, APP_TILE_NAME, timeout=RELOAD_WAIT_TIMEOUT)
    # Obs: em pre-producao o backend as vezes exibe uma tela de logon do SAP
    # NetWeaver aqui. Segundo o negocio isso e especifico do ambiente de teste e
    # nao recorrente - a solucao e fechar a aba e abrir de novo, por isso nao
    # tratamos esse caso no codigo.
    pause("Tela 'Display Change Requests' aberta")

    driver.switch_to.default_content()
    iframe = WebDriverWait(driver, RELOAD_WAIT_TIMEOUT).until(
        EC.presence_of_element_located(APP_IFRAME_LOCATOR)
    )
    driver.switch_to.frame(iframe)
    pause("Foco trocado pra dentro do iframe do app (WebDynpro)")


def open_cr(driver, cr_number):
    log.info(f"Abrindo CR {cr_number}...")

    fill_field_by_label(driver, "Change Request", cr_number)
    click_link_by_text(driver, "Continue")
    pause("Resultado da busca deveria mostrar 1 linha")

    # O numero da CR na celula da tabela costuma ser um link aninhado (nao o
    # texto direto do <td>), por isso usa o mesmo helper generico do resto do
    # fluxo em vez de um XPath rigido por texto direto do <td>.
    #
    # Esse clique abre a CR numa aba/janela NOVA (confirmado em 2026-08-21,
    # outro WebDynpro - bs_ovp_bp - dessa vez direto na janela, sem iframe
    # aninhado). Troca o foco pra ela e espera o overlay de carregamento
    # sumir antes de seguir - o resto do fluxo (Edit x4, impostos,
    # Save/Submit) acontece nessa aba nova.
    janelas_antes = driver.window_handles
    click_link_by_text(driver, cr_number)
    switch_to_new_window(driver, janelas_antes, timeout=RELOAD_WAIT_TIMEOUT)
    wait_for_wda_ready(driver)
    pause(f"CR {cr_number} deveria estar aberta agora")


def cr_esta_travada(driver):
    """
    True se a tela da CR aberta mostra aviso de lock ('locked by ...').

    Ver CLAUDE.md gotchas 7/11: clicar em qualquer "Edit"/"Details" trava a CR
    no servidor (so Save/Submit/Withdraw/Undo solta), e as vezes ela ja aparece
    travada de uma sessao anterior sem ninguem ter mexido. Numa CR travada o
    clique em "Details"/"New" nao entra em modo de edicao real e o Save/Submit
    do fim nao persiste nada - entao main() aborta a CR antes de comecar.
    """
    # FAIL CLOSED (9a revisao do Codex): antes, qualquer excecao de leitura
    # virava False = "destravada". Numa pre-condicao de seguranca isso e' o lado
    # errado do erro - entre CRs, um frame instavel bastaria pra o lote seguir
    # editando uma CR travada e nao persistir nada.
    try:
        txt = _safe_execute_script(
            driver, "return document.body.innerText || '';")
    except Exception as e:
        raise RuntimeError(
            f"cr_esta_travada: nao consegui ler a tela pra checar o lock ({type(e).__name__}: {e}). "
            f"Fail closed - nao da' pra afirmar que a CR esta destravada.")
    if txt is None:
        raise RuntimeError("cr_esta_travada: leitura da tela voltou vazia. Fail closed.")
    return "locked by" in str(txt).lower()


def _click_done(driver, timeout=WAIT_TIMEOUT):
    """
    Clica num botao "Done". Usa o mesmo criterio de New/Details
    (click_visible_candidate: candidato visivel com tamanho de botao real) e
    cai pra click_link_by_text se nao achar.

    NAO CONFIRMADO numa CR destravada ate 2026-09-09: a tela de impostos pode
    ter MAIS de um "Done" - o que confirma a LINHA do imposto e o "Done" de
    RODAPE que fecha a sub-tela inteira (ver CLAUDE.md, "Acao imediata").
    Enquanto isso nao for verificado, este helper assume que o "Done" visivel
    mais ao topo e o certo. testar_insert_tax.py resolve qual e qual.
    """
    try:
        return click_visible_candidate(driver, "Done", timeout=timeout, tamanho_max=(120, 60))
    except TimeoutException:
        return click_link_by_text(driver, "Done", timeout=timeout)


def navigate_to_tax_section(driver, hook_antes_do_clique=None, checar_linha_unica=True,
                            company_code_esperado=COMPANY_CODE_ESPERADO):
    """
    Navega da CR ja aberta ate a secao "ERP Vendor Company Code: Extended
    Withholding Tax Types", pronta pro loop de insert_all_taxes().

    Fluxo real mapeado e confirmado por screenshot em 27/08/2026 (ver
    CLAUDE.md, gotcha/secao "Status atual") - usa a acao "Details" de
    LINHA DE TABELA em duas tabelas aninhadas (ERP Vendors -> Company
    Codes), NAO um link "Edit" simples como a descricao original do negocio
    sugeria. Os 4 "Edit" simples nunca chegam nessa secao - eles levam a
    Supplier Details / Identification Numbers / Tax Numbers / Industries.

    checar_linha_unica (default True): antes de clicar "Details" em cada tabela
    ("ERP Vendors", "Company Codes"), conta as acoes "Details" e ABORTA
    (RuntimeError) se != 1.

RESPONDIDO pelo negocio (2026-09-14):
      - Negocio: "ERP vendor so tem uma secao por codigo mesmo (uma linha)" - a
        tabela "ERP Vendors" tem 1 linha, ponto. Abortar em != 1 e' o
        comportamento CERTO nas duas tabelas, nao so cautela.
      - Negocio: "Company code da empresa e X401 (Sempre esse)" - por isso a
        checagem de IDENTIDADE abaixo, nao so de quantidade.
      - Negocio (11/09): o fornecedor nao e' estendido a mais de um company code.

    company_code_esperado (default COMPANY_CODE_ESPERADO="X401"): antes de entrar
    na linha de "Company Codes", exige que o codigo apareca na tabela da secao.
    Contar "1 linha" so garante quantidade; isto garante QUAL. Sem esta checagem,
    uma CR de outro company code receberia os 64 codigos da empresa em silencio.
    Passar None desliga (so pra exploracao).

    Passe False so pra exploracao (ex: MODO="diagnostico" do testar_loop_taxes.py).

    hook_antes_do_clique: callable opcional extra, chamado com o nome da secao
    APOS a checagem de linha unica e ANTES do clique.
    """
    log.info("Navegando ate a secao de impostos...")

    def _entrar(secao):
        if hook_antes_do_clique:
            hook_antes_do_clique(secao)
        if checar_linha_unica:
            # clica o UNICO "Details" ancorado na tabela da secao (aborta se != 1)
            clicar_detalhe_secao(driver, secao)
        else:
            # exploracao: contagem informativa + clique pelo helper global
            try:
                log.info(f"secao '{secao}': {contar_details_secao(driver, secao)} 'Details'.")
            except RuntimeError as e:
                log.warning(f"secao '{secao}': contagem falhou ({e})")
            click_visible_candidate(driver, "Details")

    scroll_to_text(driver, "ERP Vendors")
    _entrar("ERP Vendors")
    pause("Tela 'ERP Vendor' aberta (Details da linha em 'ERP Vendors')")

    scroll_to_text(driver, "Company Codes")
    if company_code_esperado:
        info_cc = dump_secao(driver, "Company Codes")
        if not isinstance(info_cc, dict) or info_cc.get("erro"):
            raise RuntimeError(
                f"navigate_to_tax_section: nao consegui ler a secao 'Company Codes' pra "
                f"conferir o company code ({(info_cc or {}).get('erro', info_cc)!r}). Fail closed.")
        texto_cc = " ".join(str(x) for x in (info_cc.get("cabecalhos") or []))
        texto_cc += " " + " ".join(str(d.get("linha_txt", "")) for d in (info_cc.get("detalhes") or []))
        if company_code_esperado.upper() not in texto_cc.upper():
            raise RuntimeError(
                f"navigate_to_tax_section: esperava o company code {company_code_esperado!r} "
                f"(negocio, 14/09: 'Company code da empresa e X401, sempre esse') mas nao achei "
                f"na secao 'Company Codes'. Texto lido: {texto_cc[:300]!r}. ABORTANDO - os 64 "
                f"codigos sao da empresa e nao podem entrar em outro company code.")
        log.info(f"company code {company_code_esperado!r} confirmado na secao 'Company Codes'.")
    _entrar("Company Codes")
    pause("Tela 'ERP Vendor Company Code' aberta (Details da linha em 'Company Codes')")

    scroll_to_text(driver, "Extended Withholding Tax Types", tamanho_max=(500, 100))
    pause("Secao 'Extended Withholding Tax Types' visivel, pronta pro loop de 'New'")


# Le a tabela "ERP Vendor Company Code: Extended Withholding Tax Types" (linhas
# ja commitadas). Usado pelo preflight de idempotencia (nao reinserir um codigo
# que ja existe) e pela verificacao pos-"Done" (testar_loop_taxes.py, camada 2).
#
# Como localiza a tabela: ancora no cabecalho "Withholding Tax Type Description"
# (especifico - o cabecalho da coluna do Type e' so "Withholding Tax Type" e
# colidiria com o label da sub-tela) e sobe ate o <table>/[role=grid]. Confere o
# texto de "tabela vazia" do WDA ("The table does not contain any data" - visto
# no screenshot tax_00 de 2026-09-09).
#
# Colunas: mapeadas pelo INDICE no cabecalho (nao por "texto curto"). Cada linha
# de dados tem que produzir um Type na coluna mapeada; se cair no fallback
# heuristico, a linha vem com metodo='heuristica' e ler_linhas_impostos()
# levanta excecao (fail closed) - o DOM real da <tr> ainda nao foi capturado, so
# ha PNGs de 09/09. testar_loop_taxes.py roda um MODO diagnostico (sem escrita)
# que dumpa a estrutura; firmar o mapa de colunas aqui antes de confiar no
# parser pra autorizar escrita.
# ============================================================
# CONTRATO UNICO DA TABELA DE IMPOSTOS
# ============================================================
# Toda leitura/acao na tabela "ERP Vendor Company Code: Extended Withholding
# Tax Types" passa por aqui. Ate 2026-09-10 havia DOIS mapeamentos quase
# identicos (_LER_LINHAS_IMPOSTOS_JS e _JS_TAX_TABLE_PREAMBULO) que podiam
# divergir em silencio - um ajuste de coluna num deles nao chegava no outro.
# Unificados em 2026-09-11 (pendencia aceita da 6a revisao do Codex).
#
# Quem usa: ler_impostos_bruto/ler_linhas_impostos (leitura completa),
# _JS_CLICAR_ACAO_LINHA (Details/Delete/Edit de UMA linha), _JS_DUMP_ACOES_LINHA.
_JS_TAX_TABLE_PREAMBULO = r"""
const norm = (s) => (s || '').replace(/\u00ad/g, '').replace(/\s+/g, ' ').trim();
const COL_DESC = 'withholding tax type description';
const COL_TYPE = 'withholding tax type';

// Celula de cabecalho da coluna Description. Prefere [role=columnheader]: sem
// isso, <td>/<span> aninhados dentro de cada <th> viram "colunas" (o 1o
// diagnostico achou 15 colunas numa tabela de 4).
function acharHeaderCell() {
    for (const sel of ['[role="columnheader"], th', 'span, div, td, bdi']) {
        for (const el of document.querySelectorAll(sel)) {
            const t = norm(el.innerText).toLowerCase();
            if (!t || !t.includes(COL_DESC) || t.length > COL_DESC.length + 15) continue;
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) return el;
        }
    }
    return null;
}

// Quantas TABELAS distintas visiveis tem esse cabecalho. Deveria ser 1. Se for
// mais, o parser esta escolhendo uma em silencio - ler_linhas_impostos avisa.
function contarTabelasCandidatas() {
    const tabelas = [];
    for (const el of document.querySelectorAll('[role="columnheader"], th, span, div, td, bdi')) {
        const t = norm(el.innerText).toLowerCase();
        if (!t || !t.includes(COL_DESC) || t.length > COL_DESC.length + 15) continue;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const tb = el.closest('[role="grid"]') || el.closest('table');
        if (tb && tabelas.indexOf(tb) === -1) tabelas.push(tb);
    }
    return tabelas.length;
}

// Retorna {tabela, headerRow, idxType, idxDesc, cabecalhos, ariaRowcount}
// ou {erro: '...'}. NUNCA retorna null - quem chama testa `info.erro`.
function acharTaxTable() {
    const hc = acharHeaderCell();
    if (!hc) return {erro: 'cabecalho "Withholding Tax Type Description" nao visivel na tela'};
    let tb = hc.closest('[role="grid"]') || hc.closest('table');
    if (!tb) {
        let x = hc;
        for (let i = 0; i < 14 && x.parentElement; i++) {
            x = x.parentElement;
            if (x.querySelectorAll && x.querySelectorAll('[role="row"]').length > 1) { tb = x; break; }
        }
    }
    if (!tb) return {erro: 'nao localizei a tabela/grid em volta do cabecalho'};
    const headerRow = hc.closest('tr, [role="row"]');
    let headerCells = headerRow ? Array.from(headerRow.querySelectorAll('[role="columnheader"]')) : [];
    if (headerCells.length < 2 && headerRow) {
        headerCells = Array.from(headerRow.children).filter(c => /^(th|td)$/i.test(c.tagName));
    }
    const cabecalhos = headerCells.map(c => norm(c.innerText));
    let idxType = -1, idxDesc = -1;
    cabecalhos.forEach((h, i) => {
        const hl = h.toLowerCase();
        if (hl.includes(COL_DESC) || (hl.includes('description') && idxDesc < 0)) { if (idxDesc < 0) idxDesc = i; }
        else if (hl.includes(COL_TYPE) || /^\*?\s*withholding/i.test(h)) { if (idxType < 0) idxType = i; }
    });
    // aria-rowcount do grid WDA = total logico de linhas (inclui o header),
    // mesmo se a tabela virtualizar e nao renderizar todas no DOM. -1 =
    // ausente/desconhecido. Guard contra virtualizacao: ver ler_linhas_impostos.
    let ariaRowcount = -1;
    let g = tb;
    for (let i = 0; i < 4 && g; i++) {
        const v = g.getAttribute && g.getAttribute('aria-rowcount');
        if (v && !isNaN(parseInt(v, 10))) { ariaRowcount = parseInt(v, 10); break; }
        g = g.parentElement && g.parentElement.closest('[aria-rowcount]');
    }
    return {tabela: tb, headerRow: headerRow, idxType: idxType, idxDesc: idxDesc,
            cabecalhos: cabecalhos, ariaRowcount: ariaRowcount,
            nTabelasCandidatas: contarTabelasCandidatas()};
}

// <tr> de DADOS: nao e' o header, nao e' outra linha de cabecalho, tem altura e
// NAO e' wrapper (varios [role=row] aninhados envolvem a mesma linha real).
function linhasDeDados(info) {
    const out = [];
    for (const tr of info.tabela.querySelectorAll('tr[role="row"], [role="row"]')) {
        if (info.headerRow && tr === info.headerRow) continue;
        if (tr.querySelector('[role="columnheader"]')) continue;
        const r = tr.getBoundingClientRect();
        if (r.height === 0) continue;
        if (tr.querySelectorAll('[role="row"]').length > 0) continue;
        out.push(tr);
    }
    return out;
}

// Celulas alinhadas as colunas do cabecalho ([role=gridcell]; fallback td/th).
function celulasDaLinha(tr) {
    let cells = Array.from(tr.querySelectorAll('[role="gridcell"]'));
    if (cells.length === 0) cells = Array.from(tr.children).filter(c => /^(td|th)$/i.test(c.tagName));
    return cells;
}

function linhasDoCodigo(info, code) {
    const alvo = norm(code).toUpperCase();
    const out = [];
    for (const tr of linhasDeDados(info)) {
        const cells = celulasDaLinha(tr);
        if (info.idxType < 0 || info.idxType >= cells.length) continue;
        if (norm(cells[info.idxType].innerText).toUpperCase() === alvo) out.push(tr);
    }
    return out;
}

function mapaColunasOk(info) {
    return !!info && !info.erro && info.idxType >= 0 && info.idxDesc >= 0
        && info.idxType !== info.idxDesc;
}
"""

# Leitura COMPLETA da tabela: usa o contrato acima e so acrescenta o parse das
# celulas em {type, description, celulas, raw, metodo} + meta pro diagnostico.
_LER_LINHAS_IMPOSTOS_JS = _JS_TAX_TABLE_PREAMBULO + r"""
const info = acharTaxTable();
if (info.erro) return {erro: info.erro};

// Limite do outerHTML devolvido. Default baixo de proposito: esta leitura roda
// ~3x por codigo inserido e 20k de HTML em cada uma deixaria o log inutil. Quem
// quer o HTML inteiro (pra virar fixture do teste offline) chama
// capturar_html_tabela(), que passa um limite alto e salva em ARQUIVO.
const LIMITE_HTML = (typeof arguments[0] === 'number' && arguments[0] > 0) ? arguments[0] : 4000;

// meta vem ANTES do check de "tabela vazia" pra o MODO diagnostico enxergar a
// estrutura (cabecalhos/idx) mesmo com a tabela ainda sem nenhuma linha.
const meta = {
    cabecalhos: info.cabecalhos, idxType: info.idxType, idxDesc: info.idxDesc,
    aria_rowcount: info.ariaRowcount,
    n_tabelas_candidatas: info.nTabelasCandidatas,
    tabela_html: info.tabela.outerHTML.slice(0, LIMITE_HTML)
};

const txtTab = norm(info.tabela.innerText).toLowerCase();
if (txtTab.includes('does not contain any data')) {
    return Object.assign({linhas: [], vazio_confirmado: true}, meta);
}

const ACAO = /^(actions?|details|edit|delete|copy|remove|insert|new|c\.)$/i;
const linhas = [];
for (const tr of linhasDeDados(info)) {
    const celulas = celulasDaLinha(tr).map(c => norm(c.innerText));
    const raw = norm(tr.innerText);
    if (!raw) continue;
    if (celulas.every(t => !t)) continue;   // linha template totalmente vazia

    let type = '', description = '', metodo = '';
    if (info.idxType >= 0 && info.idxType < celulas.length && celulas[info.idxType]) {
        type = celulas[info.idxType];
        metodo = 'header-index';
        if (info.idxDesc >= 0 && info.idxDesc < celulas.length) description = celulas[info.idxDesc];
    } else {
        const uteis = celulas.filter(t => t && !ACAO.test(t));
        for (const t of uteis) { if (/^[A-Za-z0-9]{1,4}$/.test(t)) { type = t; break; } }
        for (const t of uteis) { if (t !== type && t.length > description.length) description = t; }
        metodo = 'heuristica';
    }
    linhas.push({type: type, description: description, celulas: celulas, raw: raw,
                 metodo: metodo, rowindex: tr.getAttribute('aria-rowindex') || ''});
}
return Object.assign({linhas: linhas, vazio_confirmado: false}, meta);
"""


def ler_impostos_bruto(driver, max_html=None, critico=False):
    """
    Retorno CRU do JS de leitura da tabela (cabecalhos, idxType/idxDesc,
    linhas com `metodo`, `tabela_html` truncado). Pro MODO diagnostico de
    testar_loop_taxes.py - nao aplica nenhuma checagem, so devolve o que o JS viu.
    critico=True: le pelo executor que nunca aceita dialogo (caminho do Delete).
    """
    executar = _execute_script_critico if critico else _safe_execute_script
    return executar(driver, _LER_LINHAS_IMPOSTOS_JS, int(max_html or 0))   # 0 -> JS usa 4000


def capturar_html_tabela(driver, limite=60000, nome=None):
    """
    Salva o outerHTML REAL da tabela de impostos num arquivo e devolve o caminho.

    Pra que serve: o teste offline (testar_parser_offline.py) monta um DOM
    SINTETICO e, como o Codex apontou na 8a revisao, isso da' falsa confianca -
    ele testa a logica do parser, nao a fidelidade do DOM. Com o HTML de uma
    tabela POPULADA de verdade, o teste passa a rodar contra a coisa real.

    A tentativa de extrair isso do log da rodada de 14/09 falhou: o
    `tabela_html` das leituras normais e' truncado em 4000 chars e o <thead>
    sozinho consome tudo, sem chegar nas <tr> de dados - que sao exatamente a
    parte que o sintetico inventa. Por isso aqui o limite e' alto e a saida vai
    pra ARQUIVO, nao pro log.
    """
    info = ler_impostos_bruto(driver, max_html=limite)
    if not isinstance(info, dict) or info.get("erro"):
        raise RuntimeError(f"capturar_html_tabela: {(info or {}).get('erro', info)!r}")
    html = info.get("tabela_html") or ""
    nome = nome or f"tabela_real_{datetime.now():%Y%m%d_%H%M%S}.html"
    caminho = os.path.join(LOGS_DIR, nome)
    with open(caminho, "w", encoding="utf-8", newline="") as f:
        f.write(html)
    truncou = len(html) >= limite
    log.info(f"HTML real da tabela salvo em {caminho} ({len(html)} chars, "
             f"{len(info.get('linhas') or [])} linha(s)"
             f"{', TRUNCADO - aumentar o limite' if truncou else ''}).")
    return caminho


def ler_linhas_impostos(driver, strict_rowcount=False, com_meta=False, critico=False):
    """
    Linhas ja commitadas na tabela "Extended Withholding Tax Types" como lista de
    dicts {"type", "description", "celulas": [str], "raw": str, "metodo": str}.

    strict_rowcount: exige aria-rowcount conhecido e batendo com as linhas lidas
    (usar quando a decisao depende de ter lido a tabela INTEIRA - idempotencia).

    com_meta=True: devolve (linhas, meta) em vez de so linhas. meta traz
    cabecalhos, idxType/idxDesc e aria_rowcount - usado pra INSTRUMENTAR a
    rodada real (ver inserir_uma_linha): a semantica do aria-rowcount do WDA so
    fica provada vendo o numero subir junto com as linhas em 10 e 64 codigos, e
    cada rodada custa a CR travada por horas.

    FAIL CLOSED (decisao Claude+Codex 2026-09-10, endurecida na 2a revisao):
      - RuntimeError se nao localizar a tabela;
      - RuntimeError se o mapa de colunas do cabecalho for invalido
        (idxType/idxDesc < 0 ou iguais) - vale INCLUSIVE pra tabela vazia:
        sem mapa de coluna nao da pra confiar em "codigo ausente/presente",
        e trocar pra MODO='escrita' sem firmar o dump cairia aqui em vez de
        so DEPOIS de ja ter criado o rascunho e travado a CR;
      - [] SOMENTE quando a tela confirma "The table does not contain any data"
        E o mapa de colunas e' valido;
      - RuntimeError se achou o cabecalho mas 0 linhas e sem esse texto;
      - RuntimeError se QUALQUER linha nao produziu um Type na coluna mapeada
        pelo cabecalho (metodo != 'header-index' ou type vazio) - um Type
        ambiguo faria o preflight de idempotencia classificar errado e
        reinserir um codigo que ja existe.

    critico=True: leitura pelo executor que nunca aceita dialogo (Delete).
    """
    info = ler_impostos_bruto(driver, critico=critico)
    if not isinstance(info, dict) or info.get("erro"):
        motivo = info.get("erro") if isinstance(info, dict) else f"retorno {type(info).__name__}"
        raise RuntimeError(
            f"ler_linhas_impostos: nao identifiquei a tabela de impostos ({motivo}). "
            "Fail closed - NAO assumir vazia. Rodar o MODO diagnostico."
        )
    it, idd = info.get("idxType", -1), info.get("idxDesc", -1)
    if not (isinstance(it, int) and isinstance(idd, int) and it >= 0 and idd >= 0 and it != idd):
        raise RuntimeError(
            f"ler_linhas_impostos: mapa de colunas invalido (idxType={it}, idxDesc={idd}). "
            f"Cabecalhos vistos: {info.get('cabecalhos')}. Rodar MODO='diagnostico' e "
            f"ajustar o mapeamento em _LER_LINHAS_IMPOSTOS_JS antes de qualquer escrita."
        )
    n_tab = info.get("n_tabelas_candidatas")
    if isinstance(n_tab, int) and n_tab > 1:
        # NAO levanta de proposito: nunca vimos 2 na tela, e recusar por algo
        # nao observado travaria a automacao numa CR possivelmente legitima
        # (>1 company code e' a duvida D4, pendente com o negocio). Mas tambem
        # nao passa em silencio - a proxima rodada real diz se acontece.
        log.warning(
            f"ATENCAO: {n_tab} tabelas visiveis com o cabecalho de impostos. O parser "
            f"esta lendo a PRIMEIRA. Se este aviso aparecer numa rodada real, conferir "
            f"na tela qual e' a certa ANTES de confiar no resultado.")
    linhas = info.get("linhas") or []
    if not linhas and not info.get("vazio_confirmado"):
        raise RuntimeError(
            "ler_linhas_impostos: achei o cabecalho mas 0 linhas de dados e a tela "
            "nao mostra 'table does not contain any data'. Ambiguo - fail closed."
        )
    for ln in linhas:
        if ln.get("metodo") != "header-index" or not (ln.get("type") or "").strip():
            raise RuntimeError(
                "ler_linhas_impostos: linha sem Type inequivoco pelo cabecalho "
                f"(metodo={ln.get('metodo')!r}, type={ln.get('type')!r}, raw={ln.get('raw')!r}). "
                "Fail closed - rodar o MODO diagnostico e firmar idxType/idxDesc."
            )
    # guard contra virtualizacao/paginacao (Codex 6a/7a rev): aria-rowcount do
    # grid WDA e' o total logico (com header, inclui linhas nao renderizadas);
    # -1 = ausente/desconhecido. A SEMANTICA deste controle WDA ainda nao foi
    # provada empiricamente com 3/10/64 linhas - ate la, com strict_rowcount:
    #   - rowcount conhecido e != len(linhas)+1  -> aborta (parser nao viu tudo);
    #   - rowcount ausente/-1                    -> aborta (nao da pra confiar).
    # Sem strict (leitura pos-Done da propria linha, tabela pequena): so avisa.
    rc = info.get("aria_rowcount", -1)
    rc = rc if isinstance(rc, int) else -1
    if rc >= 1 and len(linhas) != rc - 1:
        raise RuntimeError(
            f"ler_linhas_impostos: li {len(linhas)} linha(s) mas aria-rowcount={rc} "
            f"(esperava {rc - 1} de dados). Tabela virtualizada/paginada - o parser "
            f"NAO esta vendo tudo. Fail closed."
        )
    if strict_rowcount and rc < 1:
        raise RuntimeError(
            f"ler_linhas_impostos (strict): aria-rowcount={rc} (ausente/desconhecido). "
            f"Nao da pra garantir que li a tabela inteira. Fail closed - rodar o "
            f"diagnostico com 3/10/64 linhas pra firmar a semantica do rowcount."
        )
    if com_meta:
        return linhas, {
            "cabecalhos": info.get("cabecalhos"),
            "idxType": it, "idxDesc": idd,
            "aria_rowcount": rc,
            "n_tabelas_candidatas": n_tab,
            "vazio_confirmado": bool(info.get("vazio_confirmado")),
        }
    return linhas


# ============================================================
# LINHA DE IMPOSTO - inserir / verificar / deletar
# Logica validada por testar_loop_taxes.py MODO="escrita" em 2026-09-10
# (loop T1/T2/T3 + verificacao em 3 camadas). Ver CLAUDE.md.
# ============================================================

# Conta as ACOES "Details" clicaveis de uma secao (tamanho de link real), NAO
# linhas <tr>/[role=row] - varios "row" aninhados envolvem a mesma linha e cada
# um contem o mesmo <a>Details</a> (visto no diagnostico: contava 4 pra 1 real).
_JS_DUMP_SECAO = r"""
const norm = (s) => (s || '').replace(/\u00ad/g, '').replace(/\s+/g, ' ').trim();
const alvo = norm(arguments[0]).toLowerCase();

let titulo = null, menor = Infinity;
for (const el of document.querySelectorAll('h1,h2,h3,h4,div,span,bdi,a,td,th')) {
    const t = norm(el.innerText).toLowerCase();
    if (!t || t.indexOf(alvo) === -1 || t.length > alvo.length + 45) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || Math.abs(r.top) > 12000) continue;
    if (t.length < menor) { menor = t.length; titulo = el; }
}
if (!titulo) return {erro: 'titulo da secao ' + JSON.stringify(arguments[0]) + ' nao encontrado'};

let tabela = null;
for (const tb of document.querySelectorAll('table, [role="grid"]')) {
    if (!(titulo.compareDocumentPosition(tb) & 4)) continue;   // 4 = DOCUMENT_POSITION_FOLLOWING
    const r = tb.getBoundingClientRect();
    if (r.height <= 0) continue;
    const temDetails = Array.from(tb.querySelectorAll('a, span[role="button"], div[role="button"]'))
        .some(a => norm(a.innerText).toLowerCase() === 'details');
    if (temDetails) { tabela = tb; break; }
}
if (!tabela) return {erro: 'nenhuma tabela com acao "Details" apos o titulo '
    + JSON.stringify(arguments[0])};

const detalhes = [];
for (const el of tabela.querySelectorAll('a, span[role="button"], div[role="button"]')) {
    if (norm(el.innerText).toLowerCase() !== 'details') continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.width > 220 || r.height > 44) continue;
    const tr = el.closest('tr') || el.closest('[role="row"]');
    detalhes.push({
        id: el.id || '', tag: el.tagName.toLowerCase(),
        box: Math.round(r.left) + ',' + Math.round(r.top) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height),
        linha_txt: norm((tr || el).innerText).slice(0, 90)
    });
}
let cabecalhos = [];
for (const tr of tabela.querySelectorAll('tr, [role="row"]')) {
    const cs = Array.from(tr.querySelectorAll('th, td, [role="columnheader"], [role="gridcell"]'))
        .map(c => norm(c.innerText)).filter(Boolean);
    if (cs.length > 1) { cabecalhos = cs; break; }
}
return {n_details: detalhes.length, detalhes: detalhes, cabecalhos: cabecalhos,
    tabela_html: tabela.outerHTML.slice(0, 3500)};
"""


def dump_secao(driver, nome_secao):
    """Retorno cru do JS: {n_details, detalhes[], cabecalhos[], tabela_html} ou {erro}."""
    return _safe_execute_script(driver, _JS_DUMP_SECAO, nome_secao)


def contar_details_secao(driver, nome_secao):
    """Numero de acoes 'Details' clicaveis na tabela da secao. Levanta
    RuntimeError se nao achar a secao/tabela (fail closed)."""
    info = dump_secao(driver, nome_secao)
    if not isinstance(info, dict) or info.get("erro"):
        raise RuntimeError(
            f"contar_details_secao('{nome_secao}'): {(info or {}).get('erro', info)!r}"
        )
    return info.get("n_details", 0)


# Acha a tabela da secao, EXIGE exatamente 1 acao "Details" e clica ELA (nao um
# "Details" qualquer da tela) - Codex 6a revisao: contar numa secao e clicar via
# helper global pode acabar clicando outro elemento.
_JS_CLICAR_DETALHE_SECAO = _JS_DUMP_SECAO.replace(
    "return {n_details: detalhes.length, detalhes: detalhes, cabecalhos: cabecalhos,\n    tabela_html: tabela.outerHTML.slice(0, 3500)};",
    r"""
if (detalhes.length !== 1) return {erro: 'secao ' + JSON.stringify(arguments[0])
    + ' tem ' + detalhes.length + ' acoes Details (esperado 1)', n: detalhes.length};
let alvoEl = null;
for (const el of tabela.querySelectorAll('a, span[role="button"], div[role="button"]')) {
    if (norm(el.innerText).toLowerCase() !== 'details') continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.width > 220 || r.height > 44) continue;
    alvoEl = el; break;
}
if (!alvoEl) return {erro: 'nao reachei o elemento Details da secao'};
alvoEl.scrollIntoView({block: 'center'});
alvoEl.click();
return {ok: true, id: alvoEl.id || ''};
""",
)


def clicar_detalhe_secao(driver, nome_secao):
    """Clica o unico 'Details' da tabela da secao. RuntimeError se != 1 ou nao achou."""
    r = _safe_execute_script(driver, _JS_CLICAR_DETALHE_SECAO, nome_secao)
    if not isinstance(r, dict) or not r.get("ok"):
        raise RuntimeError(f"clicar_detalhe_secao('{nome_secao}'): {(r or {}).get('erro', r)!r}")
    log.info(f"secao '{nome_secao}': cliquei o Details (id={r.get('id')!r}).")
    return r


# Acha a tabela de impostos + a <tr> de um codigo (pela CELULA da coluna Type
# mapeada pelo cabecalho, nao por palavra no innerText) e clica/dumpa acoes.
# arg0 = codigo, arg1 = nome da acao ("details" casa por innerText;
# "delete"/"edit" casam por atributo title, que e' onde o WDA poe o rotulo).
_JS_CLICAR_ACAO_LINHA = _JS_TAX_TABLE_PREAMBULO + r"""
const info = acharTaxTable();
if (!info || info.erro) return {erro: info && info.erro ? info.erro : 'tabela de impostos nao encontrada'};
if (!mapaColunasOk(info)) return {erro: 'mapa de colunas invalido (idxType=' + info.idxType
    + ', idxDesc=' + info.idxDesc + ') - cabecalhos: ' + JSON.stringify(info.cabecalhos)};
const trs = linhasDoCodigo(info, arguments[0]);
if (trs.length === 0) return {erro: 'nenhuma <tr> com Type==' + JSON.stringify(arguments[0])};
if (trs.length > 1) return {erro: trs.length + ' <tr> com Type==' + JSON.stringify(arguments[0]) + ' (ambiguo)'};
const acao = norm(arguments[1]).toLowerCase();
for (const a of trs[0].querySelectorAll('a, span[role="button"], div[role="button"]')) {
    const txt = norm(a.innerText).toLowerCase();
    const tit = norm(a.getAttribute('title') || '').toLowerCase();
    if (txt === acao || tit === acao) {
        a.scrollIntoView({block: 'center'});
        a.click();
        return {ok: true, id: a.id || ''};
    }
}
return {erro: 'acao ' + JSON.stringify(arguments[1]) + ' nao encontrada na <tr> de ' + JSON.stringify(arguments[0])};
"""

_JS_DUMP_ACOES_LINHA = _JS_TAX_TABLE_PREAMBULO + r"""
const info = acharTaxTable();
if (!info || info.erro) return {erro: info && info.erro ? info.erro : 'tabela de impostos nao encontrada'};
if (!mapaColunasOk(info)) return {erro: 'mapa de colunas invalido'};
const trs = linhasDoCodigo(info, arguments[0]);
if (trs.length !== 1) return {erro: trs.length + ' <tr> com Type==' + JSON.stringify(arguments[0])};
const acoes = [];
for (const el of trs[0].querySelectorAll('a, span, div, img, button')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    acoes.push('<' + el.tagName.toLowerCase() + '> id=' + JSON.stringify(el.id)
        + ' title=' + JSON.stringify(el.getAttribute('title') || '')
        + ' aria=' + JSON.stringify(el.getAttribute('aria-label') || '')
        + ' lsevents=' + JSON.stringify((el.getAttribute('lsevents') || '').slice(0, 40))
        + ' txt=' + JSON.stringify(norm(el.innerText).slice(0, 20)));
}
return {acoes: acoes, raw: norm(trs[0].innerText).slice(0, 120)};
"""


# Clique do DELETE, so' pelo executor critico (NO-GO do Codex, 28/09, pro
# limpar_cr_teste): o _JS_CLICAR_ACAO_LINHA clica a PRIMEIRA acao que casar e
# roda pelo _safe_execute_script, que ACEITA um confirm() e repete o clique.
# Aqui: exatamente 1 tabela candidata, exatamente 1 <tr> do codigo e exatamente
# 1 controle Delete VISIVEL na linha (no' aninhado dentro de outro candidato
# conta como o mesmo controle). Qualquer outra contagem: erro, sem clique.
_JS_CLICAR_DELETE_LINHA = _JS_TAX_TABLE_PREAMBULO + r"""
const info = acharTaxTable();
if (!info || info.erro) return {erro: info && info.erro ? info.erro : 'tabela de impostos nao encontrada'};
if (info.nTabelasCandidatas !== 1) return {erro: 'esperava exatamente 1 tabela de impostos, achei '
    + info.nTabelasCandidatas};
if (!mapaColunasOk(info)) return {erro: 'mapa de colunas invalido - cabecalhos: ' + JSON.stringify(info.cabecalhos)};
const trs = linhasDoCodigo(info, arguments[0]);
if (trs.length !== 1) return {erro: trs.length + ' <tr> com Type==' + JSON.stringify(arguments[0])
    + ' (esperado 1)'};
let cands = [];
for (const a of trs[0].querySelectorAll('a, button, span[role="button"], div[role="button"]')) {
    const r = a.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const txt = norm(a.innerText).toLowerCase();
    const tit = norm(a.getAttribute('title') || '').toLowerCase();
    if (txt === 'delete' || tit === 'delete') cands.push(a);
}
cands = cands.filter(a => !cands.some(b => b !== a && b.contains(a)));
if (cands.length !== 1) return {erro: 'esperava exatamente 1 controle Delete na <tr> de '
    + JSON.stringify(arguments[0]) + ', achei ' + cands.length, ids: cands.map(c => c.id || '')};
cands[0].scrollIntoView({block: 'center'});
cands[0].click();
return {ok: true, id: cands[0].id || ''};
"""


def _clicar_acao_linha(driver, code, acao):
    """Clica uma acao ('Details'/'Delete'/'Edit') na <tr> do codigo na tabela de
    impostos. Retorna o dict do JS ({ok:true,...} ou {erro:...})."""
    return _safe_execute_script(driver, _JS_CLICAR_ACAO_LINHA, code, acao)


def _reler_valor_campo(driver, label_text, timeout=5):
    """Rele um input sem manter WebElement entre a busca e a leitura.

    O primeiro comando extrai somente o id. O segundo re-busca esse id e le o
    valor dentro do MESMO JS. Se o WDA trocar o no' entre os dois, recomeca pelo
    label; nunca repassa uma referencia velha (gotcha 17 / 29a entrada).
    """
    fim = time.time() + timeout
    while time.time() < fim:
        campo = find_input_by_label(driver, label_text)
        if campo is None:
            time.sleep(0.2)
            continue
        try:
            campo_id = _safe_execute_script(
                driver, "return arguments[0].id || '';", campo) or ""
            if campo_id:
                info = _safe_execute_script(driver, """
                    const el = document.getElementById(arguments[0]);
                    return el ? {achou: true, valor: el.value || ''}
                              : {achou: false, valor: null};
                """, campo_id)
                if isinstance(info, dict) and info.get("achou"):
                    return info.get("valor", "")
            else:
                # Fallback pra inputs sem id. Ainda tolera stale e re-acha pelo
                # label; os campos WDA reais observados sempre tem id.
                return _safe_execute_script(
                    driver, "return arguments[0].value || '';", campo)
        except StaleElementReferenceException:
            pass
        time.sleep(0.2)
    return None


def _tela_contem(driver, texto):
    try:
        return texto in (_safe_execute_script(driver, "return document.body.innerText || '';") or "")
    except Exception:
        return False


def validar_linha_imposto(driver, code):
    """
    Abre o 'Details' da linha commitada de `code`, confere que Type/Code/checkbox
    persistiram, e fecha (o Done de saida re-commita os MESMOS valores - nao
    altera nada). Levanta RuntimeError se algo nao bater.

    Reusavel: e' a "camada 3" da insercao E o check de idempotencia (nao basta o
    Type aparecer na tabela - a tabela nao mostra Code nem checkbox; so abrindo o
    Details da pra afirmar que a linha esta COMPLETA).
    """
    scroll_to_text(driver, "Extended Withholding Tax Types", tamanho_max=(500, 100))
    r = _clicar_acao_linha(driver, code, "Details")
    if not isinstance(r, dict) or not r.get("ok"):
        raise RuntimeError(f"validar_linha_imposto ({code}): nao cliquei 'Details': "
                           f"{(r or {}).get('erro', r)!r}")
    try:
        wait_for_wda_ready(driver, timeout=20)
    except TimeoutException:
        time.sleep(2)
    if not _tela_contem(driver, "Extended Withholding Tax Type"):
        raise RuntimeError(f"validar_linha_imposto ({code}): 'Details' nao abriu a sub-tela.")

    v_type = _reler_valor_campo(driver, "Withholding Tax Type")
    v_code = _reler_valor_campo(driver, "Withholding Tax Code")
    box = _safe_execute_script(driver, _CHECKBOX_PERTO_DO_LABEL_JS, "Subject to Withholding Tax")
    chk = None
    if box is not None:
        chk = _safe_execute_script(
            driver,
            "return arguments[0].getAttribute('aria-checked') === 'true' || arguments[0].checked === true;",
            box,
        )
    problemas = []
    if (v_type or "").upper() != code.upper():
        problemas.append(f"Type persistido={v_type!r}")
    if (v_code or "").upper() != code.upper():
        problemas.append(f"Code persistido={v_code!r}")
    if chk is not True:
        problemas.append(f"checkbox persistido={chk!r}")

    _click_done(driver)
    try:
        wait_for_wda_ready(driver, timeout=20)
    except TimeoutException:
        time.sleep(2)
    if not _tela_contem(driver, "ERP Vendor Company Code: Extended Withholding Tax Types"):
        problemas.append("apos fechar o Details nao voltei pra tela da tabela de impostos")

    if problemas:
        raise RuntimeError(f"validar_linha_imposto ({code}) falhou: " + "; ".join(problemas))
    log.info(f"[{code}] linha completa (Type/Code/checkbox persistiram).")


def _tipos_multiset(linhas):
    return sorted((ln.get("type") or "").upper() for ln in linhas)


def _prog(cb, **evento):
    """
    Avisa o chamador da fase atual. Existe pro harness ter post-mortem e
    screenshots sem duplicar a logica de insercao (8a revisao do Codex, ponto 5:
    ao delegar pro modulo, o teste perdeu a fase corrente e os prints por passo).
    NUNCA deixa o callback derrubar a insercao - screenshot que falha nao pode
    abortar uma escrita que ja esta em andamento.
    """
    if cb is None:
        return
    try:
        cb(evento)
    except Exception:
        log.warning(f"callback de progresso falhou em {evento!r}", exc_info=True)


class SapIndisponivel(RuntimeError):
    """Prova positiva de que nao da para continuar (aviso de desligamento,
    expiracao informada, volta a autenticacao, erro da aplicacao, janela ou
    sessao do WebDriver perdida). Para a CR e o lote na hora, sem esperar o
    prazo, e nunca autoriza clique nenhum (gotcha 26). `categoria` diz QUAL
    sinal foi: interromper nao e' diagnosticar queda do servidor (pesquisas do
    Codex, docs/PESQUISAS-SAP-ETAPA1-2026-10-13.md)."""

    def __init__(self, mensagem, categoria="indisponibilidade"):
        super().__init__(mensagem)
        self.categoria = categoria


# Sinais especificos (gotcha 26), por categoria. So frases que nao aparecem numa
# tela de CR normal: palavra solta ("error", "timeout", "terminated") nao serve.
# Fontes e alcance de cada uma em docs/PESQUISAS-SAP-ETAPA1-2026-10-13.md; a
# 1a e' o aviso visto no Launchpad de producao em 13/10 ~11:02.
_FRASES_SAP_INDISPONIVEL = (
    ("instance is closing down", "aviso de desligamento"),
    ("session has expired", "expiracao informada pelo SAP"),
    ("session expired", "expiracao informada pelo SAP"),
    ("session has timed out", "expiracao informada pelo SAP"),
    ("session timed out", "expiracao informada pelo SAP"),
    ("session was terminated", "expiracao informada pelo SAP"),
    ("session has been terminated", "expiracao informada pelo SAP"),
    ("session no longer exists", "expiracao informada pelo SAP"),
    ("abap session was closed after timeout", "expiracao informada pelo SAP"),
    ("sap internal server error", "erro da aplicacao"),
)

# Volta a autenticacao: o host da Microsoft, ou o formulario de logon do SAP
# com usuario E senha VISIVEIS (campo isolado ou oculto e' evidencia fraca).
_JS_SINAL_SAP_INDISPONIVEL = r"""
const host = (location.host || '').toLowerCase();
if (host === 'login.microsoftonline.com')
    return {categoria: 'retorno a autenticacao', detalhe: 'login da Microsoft'};
const visivel = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
};
const usuario = document.querySelector('input[name="sap-user"]');
const senha = document.querySelector('input[name="sap-password"][type="password"]');
if (visivel(usuario) && visivel(senha))
    return {categoria: 'retorno a autenticacao', detalhe: 'tela de logon do SAP'};
const texto = ((document.body && document.body.innerText) || '')
    .replace(/\u00ad/g, '').replace(/\s+/g, ' ').toLowerCase();
for (const [frase, categoria] of arguments[0]) {
    if (texto.includes(frase)) return {categoria: categoria, detalhe: 'a tela mostrou "' + frase + '"'};
}
return null;
"""


def sinal_sap_indisponivel(driver):
    """(categoria, detalhe) se a janela da CR mostra prova positiva de que nao
    da para continuar; None se nao. Janela/sessao do WebDriver perdida vira
    SapIndisponivel direto."""
    try:
        r = _safe_execute_script(driver, _JS_SINAL_SAP_INDISPONIVEL,
                                 [list(par) for par in _FRASES_SAP_INDISPONIVEL])
    except NoSuchWindowException as e:
        raise SapIndisponivel(f"a janela da CR fechou ({type(e).__name__}). Nada foi "
                              f"gravado.", "janela perdida") from e
    except InvalidSessionIdException as e:
        raise SapIndisponivel(f"a sessao do navegador (WebDriver) ficou invalida "
                              f"({type(e).__name__}). Nada foi gravado.",
                              "sessao WebDriver invalida") from e
    if isinstance(r, dict) and r.get("categoria"):
        return r["categoria"], r.get("detalhe") or ""
    return None


def _texto_da_tela(driver, limite=600):
    """Texto visivel da janela, compacto, so pro log de uma falha."""
    try:
        texto = _safe_execute_script(
            driver, "return (document.body && document.body.innerText) || '';") or ""
    except Exception as e:
        return f"(nao consegui ler a tela: {type(e).__name__})"
    return " ".join(texto.split())[:limite]


def _problema_linha_nova(linhas_antes, linhas, code):
    """None se a tabela mostra o resultado completo do Done de `code`: exatamente
    uma linha nova com esse Type e Description preenchida, e as demais iguais as
    de antes (Type e Description). Senao, o que nao bateu."""
    code = code.upper()

    def chave(ln):
        return ((ln.get("type") or "").upper(), (ln.get("description") or "").strip())

    novas = [ln for ln in linhas if chave(ln)[0] == code]
    if len(novas) != 1:
        return f"esperava 1 linha {code}, achei {len(novas)}"
    if not chave(novas[0])[1]:
        return f"a linha {code} esta sem Description"
    antes = Counter(chave(ln) for ln in linhas_antes)
    resto = Counter(chave(ln) for ln in linhas if chave(ln)[0] != code)
    if resto != antes:
        return (f"as outras linhas mudaram: sairam {sorted((antes - resto).elements())}, "
                f"entraram {sorted((resto - antes).elements())}")
    return None


_TOLERANCIA_CHARS_ESTAVEL = 15   # a mesma variacao tolerada pelo wait_for_wda_ready
_JS_TAMANHO_TEXTO = "return (document.body.innerText || '').length;"


def _esperar_fase(driver, rotulo, acao, t_ini, condicao=None, estabilidade=None,
                  falha_prazo=None, detalhe_prazo=None, consequencia="",
                  on_progresso=None, code=None, teto_s=None):
    """
    Laco unico das esperas do loop de insercao (gotcha 26; revisao do Codex,
    78a: NO-GO de ffcbf26). A cada INTERVALO_LOOP, nesta ordem:
      1. prazo: `teto_s` (TETO_ESPERA_SAP_S) desde `t_ini`, em time.monotonic,
         fixado uma vez; esgotado -> RuntimeError, e quem chama NAO segue;
      2. prova positiva de indisponibilidade (sinal_sap_indisponivel) ->
         SapIndisponivel na hora;
      3. SAP ocupado (blindlayer > 2 px, gotcha 9) -> continua esperando;
      4. `condicao()` -> (ok, situacao, resultado); ambiguo continua esperando;
      5. estabilidade: so devolve `resultado` quando a condicao vale e o texto
         da tela fica parado por `estabilidade` s seguidos (o criterio do
         wait_for_wda_ready, mas DENTRO deste laco).
    Por isso a sondagem de queda e o prazo valem tambem durante a estabilizacao,
    e o sucesso so sai numa volta que acabou de passar pelos passos 1 a 4 - os
    dois furos que a revisao reproduziu na versao que chamava wait_for_wda_ready
    (time.time, sem sondagem). estabilidade=0: basta o SAP livre e a condicao.
    Avisa no log a cada AVISO_ESPERA_S. NUNCA clica em nada.
    """
    teto_s = TETO_ESPERA_SAP_S if teto_s is None else teto_s
    estabilidade = ESTABILIDADE_LOOP if estabilidade is None else estabilidade
    fim = t_ini + teto_s
    proximo_aviso = t_ini + AVISO_ESPERA_S
    situacao = "aguardando o SAP"
    estavel_desde, tamanho_anterior = None, -1
    try:
        while True:
            agora = time.monotonic()
            if agora >= fim:
                log.error(f"{rotulo}: tela no fim da espera de {acao}: "
                          f"{_texto_da_tela(driver)!r}")
                raise RuntimeError(
                    f"{rotulo}: {falha_prazo or acao + ' nao se concluiu'} em "
                    f"{teto_s:.0f} s (ultima situacao: {situacao}"
                    f"{detalhe_prazo() if detalhe_prazo else ''}). {consequencia}".rstrip())
            sinal = sinal_sap_indisponivel(driver)
            if sinal:
                categoria, detalhe = sinal
                log.error(f"{rotulo}: {categoria} ({detalhe}) durante {acao}. "
                          f"Tela: {_texto_da_tela(driver)!r}")
                raise SapIndisponivel(
                    f"{rotulo}: {categoria} ({detalhe}) durante {acao}. Parei sem clicar "
                    f"em mais nada e antes do Save: nada foi gravado.", categoria)
            if _safe_execute_script(driver, _JS_BLINDLAYER_BLOQUEANDO):
                situacao, estavel_desde = "SAP ocupado", None
            else:
                ok, sit, resultado = condicao() if condicao else (True, None, None)
                tamanho = _safe_execute_script(driver, _JS_TAMANHO_TEXTO) or 0
                parado = tamanho > 0 and abs(tamanho - tamanho_anterior) <= _TOLERANCIA_CHARS_ESTAVEL
                tamanho_anterior = tamanho
                if ok and estabilidade <= 0:
                    return resultado
                if ok and parado:
                    if estavel_desde is None:
                        estavel_desde = agora
                    elif agora - estavel_desde >= estabilidade:
                        return resultado
                    situacao = "conferindo se a tela parou"
                else:
                    estavel_desde = None
                    situacao = sit or "a tela ainda esta mudando"
            if agora >= proximo_aviso:
                log.info(f"{rotulo}: SAP ainda processando {acao} - aguardando ha "
                         f"{agora - t_ini:.0f} s, no maximo {teto_s:.0f} s ({situacao}).")
                _prog(on_progresso, code=code, fase=f"aguardando o SAP ({acao})",
                      segundos=round(agora - t_ini))
                while proximo_aviso <= agora:
                    proximo_aviso += AVISO_ESPERA_S
            time.sleep(max(0.05, min(INTERVALO_LOOP, fim - time.monotonic())))
    except NoSuchWindowException as e:
        raise SapIndisponivel(f"{rotulo}: a janela da CR fechou durante {acao} "
                              f"({type(e).__name__}). Nada foi gravado.",
                              "janela perdida") from e
    except InvalidSessionIdException as e:
        raise SapIndisponivel(f"{rotulo}: a sessao do navegador (WebDriver) ficou "
                              f"invalida durante {acao} ({type(e).__name__}). Nada foi "
                              f"gravado.", "sessao WebDriver invalida") from e


def _subtela_pronta(driver):
    """Condicao do New: a sub-tela abriu e os dois campos aceitam digitacao.
    "Withholding Tax Code" so existe na sub-tela: a tabela nao tem esse label."""
    for label in ("Withholding Tax Type", "Withholding Tax Code"):
        r = _safe_execute_script(driver, _JS_CAMPO_POR_LABEL, label, None)
        if not isinstance(r, dict) or not r.get("achou"):
            return False, f"a sub-tela ainda nao abriu ('{label}' fora da tela)", None
        if not r.get("editavel"):
            return False, f"'{label}' ainda bloqueado", None
    return True, None, None


def aguardar_resultado_done(driver, code, linhas_antes, t_done, cr_number=None,
                            on_progresso=None, teto_s=None):
    """
    Depois do UNICO clique em Done de `code`, espera a tabela voltar com o
    resultado completo (_problema_linha_nova), com a tela parada, e devolve
    (linhas, meta). Gotcha 26: o Done de producao passou de 20 s com o SAP
    ocupado o tempo todo. O prazo (TETO_ESPERA_SAP_S) conta de `t_done`, que e'
    marcado quando o comando de clique RETORNA: o tempo do proprio comando fica
    fora. Tabela ausente ou sem o resultado e' ambiguo e continua esperando; so
    prova positiva para antes (_esperar_fase). Nunca clica: um 2o Done poderia
    duplicar a linha. Levanta SapIndisponivel ou RuntimeError; quem chama para
    antes do Save (fail closed).
    """
    rotulo = f"[{cr_number}] {code}" if cr_number else f"[{code}]"
    estado = {"ultima_leitura": "", "avisou": False}

    def resultado_completo():
        try:
            linhas, meta = ler_linhas_impostos(driver, com_meta=True)
        except RuntimeError as e:
            estado["ultima_leitura"] = str(e)
            return False, "a tabela ainda nao voltou", None
        estado["ultima_leitura"] = ""
        problema = _problema_linha_nova(linhas_antes, linhas, code)
        if problema and not estado["avisou"]:
            estado["avisou"] = True
            log.warning(f"{rotulo}: a tabela voltou sem o resultado esperado ({problema}). "
                        f"Continuo esperando, sem clicar em nada.")
        return problema is None, problema, (linhas, meta)

    linhas, meta = _esperar_fase(
        driver, rotulo, "o Done", t_done, condicao=resultado_completo,
        falha_prazo="o resultado do Done nao se confirmou",
        detalhe_prazo=lambda: (f"; ultima leitura: {estado['ultima_leitura'][:200]}"
                               if estado["ultima_leitura"] else ""),
        consequencia="Foi clicado UM Done; parei antes do Save, nada foi gravado.",
        on_progresso=on_progresso, code=code, teto_s=teto_s)
    espera = time.monotonic() - t_done
    if espera >= AVISO_ESPERA_S:
        log.info(f"{rotulo}: resultado do Done confirmado depois de {espera:.0f} s.")
    return linhas, meta


def inserir_uma_linha(driver, code, verificar_profundo=False, cr_number=None,
                      on_progresso=None):
    """
    Insere UM codigo na tabela de impostos com verificacao em camadas:
      1) na sub-tela, antes do Done: value de Type/Code == code e checkbox
         marcado, relidos DEPOIS de o SAP terminar o processamento;
      2) apos o Done: aguardar_resultado_done - exatamente 1 linha nova com
         type==code e Description nao-vazia, e as demais linhas iguais as de
         antes, com a tela estavel;
      3) (se verificar_profundo) valida_linha_imposto() - abre o Details e
         confere Code/checkbox persistidos.
    Cada espera e' uma fase de _esperar_fase (gotcha 26): prazo monotonico
    proprio, aviso periodico, parada por prova positiva, e prazo esgotado
    INTERROMPE - nunca vira "dorme 2 s e segue". Um New e um Done por codigo,
    nunca repetidos. Levanta RuntimeError (SapIndisponivel, se houve prova de
    indisponibilidade) em qualquer camada que falhar.
    """
    rotulo = f"[{cr_number}] {code}" if cr_number else f"[{code}]"
    sem_done = "Nenhum Done foi clicado neste codigo; parei antes do Save, nada foi gravado."
    t0 = time.monotonic()
    _prog(on_progresso, code=code, fase="preflight da linha")
    scroll_to_text(driver, "Extended Withholding Tax Types", tamanho_max=(500, 100))
    linhas_antes = ler_linhas_impostos(driver)

    _prog(on_progresso, code=code, fase="New (abrir sub-tela)")
    click_visible_candidate(driver, "New")
    # a sub-tela abriu, o SAP liberou e os dois campos aceitam digitacao, com a
    # tela parada - so entao preenche (antes: 20 s, dormia 2 s e seguia)
    _esperar_fase(driver, rotulo, "o New", time.monotonic(),
                  condicao=lambda: _subtela_pronta(driver),
                  falha_prazo="a sub-tela do New nao ficou pronta", consequencia=sem_done,
                  on_progresso=on_progresso, code=code)
    t_new = time.monotonic()

    _prog(on_progresso, code=code, fase="preencher Type/Code/checkbox")
    fill_field_by_label(driver, "Withholding Tax Type", code)
    _esperar_fase(driver, rotulo, "o campo Type", time.monotonic(), estabilidade=0,
                  falha_prazo="o SAP nao liberou a tela depois do campo Type",
                  consequencia=sem_done, on_progresso=on_progresso, code=code)
    fill_field_by_label(driver, "Withholding Tax Code", code)
    _esperar_fase(driver, rotulo, "o campo Code", time.monotonic(), estabilidade=0,
                  falha_prazo="o SAP nao liberou a tela depois do campo Code",
                  consequencia=sem_done, on_progresso=on_progresso, code=code)
    check_checkbox_by_label(driver, "Subject to Withholding Tax")
    t_campos = time.monotonic()

    # (Codex 7a rev) espera o round-trip do WDA terminar ANTES do Done - o
    # aria-checked=true pode refletir estado local enquanto o servidor ainda nao
    # confirmou. Depois rele checkbox, Type e Code; qualquer divergencia aqui,
    # com o SAP ja parado, e' falha (antes: 15 s, dormia 2 s e seguia).
    _esperar_fase(driver, rotulo, "o preenchimento", time.monotonic(),
                  falha_prazo="o SAP nao terminou de processar o preenchimento",
                  consequencia=sem_done, on_progresso=on_progresso, code=code)
    if _checkbox_marcado_perto_do_label(driver, "Subject to Withholding Tax") is not True:
        raise RuntimeError(f"camada 1 ({code}): checkbox 'Subject to Withholding Tax' nao "
                           f"esta marcado depois do round-trip do WDA.")

    v_type = _reler_valor_campo(driver, "Withholding Tax Type")
    v_code = _reler_valor_campo(driver, "Withholding Tax Code")
    if (v_type or "").upper() != code.upper() or (v_code or "").upper() != code.upper():
        raise RuntimeError(f"camada 1 ({code}): campos leram Type={v_type!r} Code={v_code!r}")

    # A fase abaixo e' a unica ambigua do fluxo: se cair aqui, a linha PODE ter
    # sido commitada mesmo sem aparecer na lista de confirmados. O harness
    # destaca isso no post-mortem.
    _prog(on_progresso, code=code, fase="Done da linha - EM ANDAMENTO (pode commitar)")
    t_pre_done = time.monotonic()
    _click_done(driver)
    # Gotcha 26: o resultado do Done e' esperado pelo proprio resultado, com prazo
    # desde este clique - nao mais por 20 s fixos seguidos de uma leitura so.
    linhas, meta = aguardar_resultado_done(driver, code, linhas_antes, time.monotonic(),
                                           cr_number=cr_number, on_progresso=on_progresso)
    t_done = time.monotonic()
    _prog(on_progresso, code=code, fase="Done da linha - retornou")
    nova = next(ln for ln in linhas if (ln.get("type") or "").upper() == code.upper())
    log.info(f"[{code}] inserido (camada 2 OK, {len(linhas_antes)}->{len(linhas)} linhas, "
             f"desc={nova.get('description')!r}).")
    # INSTRUMENTACAO (ponto 2 do Codex): o aria-rowcount do WDA so fica provado
    # vendo ele subir junto com as linhas ate 10 e 64. Uma linha por codigo no
    # log responde isso sem gastar outra janela de CR destravada.
    log.info(f"[{code}] rowcount: aria-rowcount={meta.get('aria_rowcount')} "
             f"linhas_lidas={len(linhas)} rowindexes={[l.get('rowindex') for l in linhas]}")

    if verificar_profundo:
        _prog(on_progresso, code=code, fase="camada 3 (Details)")
        validar_linha_imposto(driver, code)

    # Tempo de parede por fase (SAP + rede + comandos + esperas da automacao, nao
    # so servidor): base da otimizacao da Etapa 2, que precisa de numero medido.
    agora = time.monotonic()
    log.info(f"[{code}] concluido em {agora - t0:.1f}s (ate o New "
             f"{t_new - t0:.1f}s, campos {t_campos - t_new:.1f}s, espera antes do Done "
             f"{t_pre_done - t_campos:.1f}s, Done {t_done - t_pre_done:.1f}s).")
    _prog(on_progresso, code=code, fase="confirmado", segundos=round(agora - t0, 1))
    # registra DEPOIS das camadas passarem (camadas 1-2 sempre; camada 3 so na
    # 1a linha, quando verificar_profundo_1a=True). O delta exato da camada 2 ja
    # prova que foi ESTA execucao que criou o Type - e' o que o registro precisa
    # garantir pro Delete (correcao documental da 8a revisao do Codex). - o registro e' o que
    # autoriza limpar_impostos a deletar esta linha (ver _INSERIDOS_NESTA_EXECUCAO).
    _registrar_insercao(cr_number, code)


# ---- Registro do que ESTA execucao inseriu -------------------------
# limpar_impostos so aceita codigos daqui. Sem isso, um cleanup podia apagar
# linha PRE-EXISTENTE da CR (dado de negocio), nao so o rascunho que a gente
# acabou de criar. Ate a 7a revisao do Codex isso era so um comentario de
# contrato - agora e' imposto em codigo. Chave = numero da CR (o mesmo processo
# pode rodar varias CRs em sequencia).
_INSERIDOS_NESTA_EXECUCAO = {}


def _registrar_insercao(cr_number, code):
    if cr_number is None:
        return
    _INSERIDOS_NESTA_EXECUCAO.setdefault(str(cr_number), set()).add(str(code).upper())


def _desregistrar_insercao(cr_number, code):
    if cr_number is None:
        return
    _INSERIDOS_NESTA_EXECUCAO.get(str(cr_number), set()).discard(str(code).upper())


def inseridos_nesta_execucao(cr_number):
    """Codigos que ESTA execucao inseriu e confirmou na CR (ordem alfabetica)."""
    return sorted(_INSERIDOS_NESTA_EXECUCAO.get(str(cr_number), set()))


def ler_tabela_impostos_integral(driver):
    """Types da tabela com leitura INTEGRAL e CRITICA: aria-rowcount conhecido e
    batendo, exatamente 1 tabela candidata, executor que nunca aceita dialogo.
    E' a leitura que decide e confere o que o Delete apaga (Codex, 28/09): uma
    leitura parcial ou da tabela errada nao pode entrar no delta, e um dialogo
    tardio nao pode ser aceito por ela."""
    linhas, meta = ler_linhas_impostos(driver, strict_rowcount=True, com_meta=True,
                                       critico=True)
    if meta.get("n_tabelas_candidatas") != 1:
        raise RuntimeError(f"esperava exatamente 1 tabela de impostos, achei "
                           f"{meta.get('n_tabelas_candidatas')!r}.")
    return _tipos_multiset(linhas)


# ---- Delete de linha / cleanup -------------------------------------
# Contrato (Codex 6a revisao): so numa CR DESTRAVADA; so codigos desta execucao;
# antes de cada clique reler a tabela e exigir exatamente 1 <tr> pro codigo;
# depois do clique reler e exigir DELTA EXATO (so aquele codigo sumiu); parar no
# 1o no-op / modal desconhecido / delta errado. Prova apenas remocao do rascunho
# NA SESSAO (persistencia so depois de Save + reabrir a CR).

def _localizar_delete_linha(driver, code):
    """READ-ONLY: dumpa as acoes da <tr> do codigo (pra mapear/confirmar o
    <a title="Delete"> sem clicar). Retorna o dict do JS."""
    scroll_to_text(driver, "Extended Withholding Tax Types", tamanho_max=(500, 100))
    return _safe_execute_script(driver, _JS_DUMP_ACOES_LINHA, code)


# Quanto tempo esperar a linha SUMIR depois do clique no Delete. Normal: ~8 s.
# 29/09 14:52: um delete (X1, 90000005) levou mais de 17 s - a releitura unica
# pegou a tabela antes de o SAP terminar, a trava de delta parou a limpeza, e o
# X1 sumiu depois. Esperar o EFEITO (a linha sumir), nao um tempo fixo.
ESPERA_MAX_DELETE_S = 60


def deletar_linha_imposto(driver, code):
    """
    Deleta a linha de `code` via <a title="Delete"> da <tr>. Exige CR
    destravada, exatamente 1 <tr> pro codigo antes, e DELTA EXATO depois (so
    esse codigo sumiu, os outros ficaram). Levanta RuntimeError em qualquer
    desvio. Retorna a lista de Types que restaram.

    CAMINHO CRITICO (NO-GO do Codex em 28/09): este Delete nunca foi clicado no
    SAP, e se ele abrir um confirm() o _safe_execute_script ACEITARIA sozinho e
    repetiria o clique. Por isso leitura, clique, espera e releitura usam o
    executor critico: um dialogo faz levantar e FICA na tela. O portao do
    inicio pega um dialogo que tenha sobrado do delete anterior antes que as
    leituras permissivas (lock, rolagem) o aceitem.
    """
    _execute_script_critico(driver, "return true;")
    if cr_esta_travada(driver):
        raise RuntimeError("deletar_linha_imposto: CR travada - delete nao persistiria (gotcha 12).")
    scroll_to_text(driver, "Extended Withholding Tax Types", tamanho_max=(500, 100))
    tipos_antes = ler_tabela_impostos_integral(driver)
    if tipos_antes.count(code.upper()) != 1:
        raise RuntimeError(f"deletar_linha_imposto ({code}): esperava 1 linha, "
                           f"tabela tem {tipos_antes.count(code.upper())}.")

    r = _execute_script_critico(driver, _JS_CLICAR_DELETE_LINHA, code)
    if not isinstance(r, dict) or not r.get("ok"):
        # Um confirm() aberto pelo proprio clique faz o execute devolver None em
        # vez de levantar (medido no caso 36). O portao critico transforma isso
        # na mensagem do dialogo - sem aceitar nada.
        _execute_script_critico(driver, "return true;")
        raise RuntimeError(f"deletar_linha_imposto ({code}): clique recusado: "
                           f"{(r or {}).get('erro', r)!r}")
    try:
        wait_for_wda_ready(driver, timeout=15, critico=True)
    except TimeoutException:
        pass

    esperado = list(tipos_antes)          # ja ordenado por _tipos_multiset
    esperado.remove(code.upper())         # tira exatamente 1 ocorrencia
    # Relê ate a linha sumir (ou ate ESPERA_MAX_DELETE_S). Para NA HORA se a
    # tabela mudou de outro jeito (delta errado) ou se abriu um dialogo (o
    # portao critico levanta, sem aceitar). Uma leitura que falha no meio de um
    # redesenho conta como "ainda nao" ate o prazo; no prazo, o erro sobe.
    prazo = time.time() + ESPERA_MAX_DELETE_S
    while True:
        _execute_script_critico(driver, "return true;")
        try:
            tipos_depois, erro_leitura = ler_tabela_impostos_integral(driver), None
        except RuntimeError as e:
            tipos_depois, erro_leitura = None, e
        if tipos_depois == esperado:
            break
        if tipos_depois is not None and tipos_depois != tipos_antes:
            break                         # mudou, mas nao so' o codigo -> delta errado
        if time.time() >= prazo:
            if erro_leitura is not None:
                raise erro_leitura
            break                         # a linha nunca sumiu -> delta errado
        time.sleep(1)
    if tipos_depois != esperado:
        try:
            driver.save_screenshot(os.path.join(
                PRINTS_DIR, f"delete_delta_errado_{code}_{datetime.now():%Y%m%d_%H%M%S}.png"))
        except Exception:
            pass
        raise RuntimeError(
            f"deletar_linha_imposto ({code}): delta errado.\n  antes={tipos_antes}\n  "
            f"depois={tipos_depois}\n  esperado={esperado}"
        )
    log.info(f"[{code}] delete OK (linha removida da sessao; {len(tipos_antes)}->{len(tipos_depois)}).")
    return tipos_depois


def limpar_impostos(driver, codes, cr_number=None, forcar=False):
    """
    Deleta as linhas de `codes` (rascunhos DESTA execucao) uma a uma, re-lendo a
    tabela e exigindo delta exato a cada passo (detalhe: deletar_linha_imposto).
    Para no 1o desvio. So numa CR destravada. Retorna a lista de Types restantes.

    IMPOSICAO (7a revisao do Codex): so deleta codigo que ESTA execucao inseriu
    e confirmou (registro em _INSERIDOS_NESTA_EXECUCAO, alimentado por
    inserir_uma_linha). Deletar uma linha que ja existia na CR apagaria dado de
    negocio - por isso passar `cr_number` e' obrigatorio. `forcar=True` pula a
    checagem e so deve ser usado com decisao explicita do operador.
    """
    codes = [str(c).strip().upper() for c in codes if str(c).strip()]
    if not codes:
        raise ValueError("limpar_impostos: lista de codigos vazia.")
    if not forcar:
        if cr_number is None:
            raise ValueError(
                "limpar_impostos: passe cr_number (pra conferir que os codigos sao desta "
                "execucao) ou forcar=True com decisao explicita do operador.")
        permitidos = set(_INSERIDOS_NESTA_EXECUCAO.get(str(cr_number), set()))
        alheios = [c for c in codes if c not in permitidos]
        if alheios:
            raise RuntimeError(
                f"limpar_impostos: {alheios} NAO foram inseridos por esta execucao na CR "
                f"{cr_number} (esta execucao inseriu: {sorted(permitidos) or '(nenhum)'}). "
                f"Deletar linha pre-existente apagaria dado de negocio - fail closed. "
                f"Se for mesmo pra deletar, chame com forcar=True.")
    if cr_esta_travada(driver):
        raise RuntimeError("limpar_impostos: CR travada - abortando (delete nao persistiria).")
    for code in codes:
        deletar_linha_imposto(driver, code)
        _desregistrar_insercao(cr_number, code)   # ja saiu da tabela: some do registro
    restantes = ler_tabela_impostos_integral(driver)   # ultima prova antes do Save
    log.info(f"limpar_impostos: {len(codes)} deletado(s). Restam na tabela: {restantes or '(nada)'}")
    return restantes


def insert_all_taxes(driver, cr_number, codes=None, verificar_profundo_1a=True,
                     reexec=None, on_progresso=None):
    """
    Insere os codigos de imposto (default TAX_CODES) na tabela ja aberta por
    navigate_to_tax_section, com verificacao por linha (ver inserir_uma_linha).

    Idempotencia (sintese Claude+Codex, 7a revisao):
      - tabela VAZIA (nenhum dos `codes` presente): insere tudo, custo zero de
        revalidacao. E' o caso normal (cada CR comeca vazia).
      - TODOS presentes: nada a fazer (retorna pulados=todos).
      - MISTO (alguns presentes, alguns ausentes) = re-execucao apos falha
        parcial: `insert_all_taxes` NAO adivinha. Exige `reexec`:
          * reexec="assumir_completos" - confia na checagem leve (Type unico +
            Description) pros presentes e insere o resto;
          * reexec="resume_strict" - abre o "Details" de CADA presente
            (validar_linha_imposto) antes de inserir o resto.
        Sem `reexec` num estado misto -> RuntimeError (o operador decide:
        limpar_impostos + redo limpo, ou uma das opcoes acima).

    Le a tabela com strict_rowcount=True (aborta se aria-rowcount desconhecido
    ou nao bate - nao da pra confiar em "presente/ausente" sem ter lido tudo).

    ABORTA no 1o erro. Retorna {"inseridos", "pulados", "faltavam"}.

    on_progresso: callback(evento) com a fase corrente. IMPORTANTE pro
    post-mortem: quando esta funcao LEVANTA, o resumo nao e' devolvido - quem
    quiser saber o que ja entrou deve usar `inseridos_nesta_execucao(cr_number)`,
    que e' registro de modulo e sobrevive a excecao (8a revisao do Codex).
    """
    codes = list(codes if codes is not None else TAX_CODES)
    norm_codes, vistos = [], set()
    for c in codes:
        cu = str(c).strip().upper()
        if not cu:
            continue
        if cu in vistos:
            raise ValueError(f"insert_all_taxes: codigo duplicado na lista: {cu}")
        if cu not in {t.upper() for t in TAX_CODES}:
            raise ValueError(f"insert_all_taxes: {cu} nao esta em TAX_CODES (nao e' codigo real).")
        vistos.add(cu)
        norm_codes.append(cu)
    if not norm_codes:
        raise ValueError("insert_all_taxes: lista de codigos vazia depois de normalizar.")
    if reexec not in (None, "assumir_completos", "resume_strict"):
        raise ValueError(f"insert_all_taxes: reexec invalido {reexec!r}.")

    linhas0 = ler_linhas_impostos(driver, strict_rowcount=True)
    tipos_tab = [(ln.get("type") or "").upper() for ln in linhas0]
    presentes = [c for c in norm_codes if c in tipos_tab]
    faltam = [c for c in norm_codes if c not in tipos_tab]

    if presentes and faltam and reexec is None:
        raise RuntimeError(
            f"[{cr_number}] estado MISTO: {len(presentes)} ja presente(s) {presentes} e "
            f"{len(faltam)} ausente(s) {faltam}. Re-execucao apos falha parcial - o "
            f"operador precisa decidir. Chame de novo com reexec='resume_strict' (abre "
            f"Details de todos os presentes) OU reexec='assumir_completos' (checagem "
            f"leve) OU limpe com limpar_impostos({presentes}) e rode limpo."
        )

    pulados = []
    for c in presentes:
        ocorr = [ln for ln in linhas0 if (ln.get("type") or "").upper() == c]
        leve_ok = len(ocorr) == 1 and (ocorr[0].get("description") or "").strip()
        if reexec == "resume_strict" or not leve_ok:
            log.info(f"[{cr_number}] validando {c} com Details "
                     f"({'resume_strict' if reexec == 'resume_strict' else 'checagem leve falhou'}).")
            validar_linha_imposto(driver, c)
        pulados.append(c)
    if pulados:
        log.info(f"[{cr_number}] {len(pulados)} ja presente(s), pulando: {pulados}")
    log.info(f"[{cr_number}] inserindo {len(faltam)}: {faltam}")

    inseridos = []
    t_loop = time.time()
    for i, code in enumerate(faltam, start=1):
        try:
            _prog(on_progresso, code=code, fase="inicio", i=i, n=len(faltam))
            inserir_uma_linha(driver, code, cr_number=cr_number,
                              verificar_profundo=(i == 1 and verificar_profundo_1a),
                              on_progresso=on_progresso)
            inseridos.append(code)
            # previsao pelos tempos DESTA CR (o SAP varia de uma hora para outra)
            media = (time.time() - t_loop) / i
            if i < len(faltam):
                log.info(f"[{cr_number}] {i}/{len(faltam)} ({code}) OK. Media {media:.1f} s "
                         f"por codigo; faltam cerca de "
                         f"{media * (len(faltam) - i) / 60:.0f} min para os codigos desta CR.")
            else:
                # "faltam 0 min" sugeriria a CR pronta (revisao do Codex): nao esta
                log.info(f"[{cr_number}] {i}/{len(faltam)} ({code}) OK. Codigos inseridos "
                         f"(media {media:.1f} s por codigo); a CR ainda NAO terminou: "
                         f"seguem as conferencias finais.")
            pause(f"{code} ({i}/{len(faltam)}) inserido")
        except Exception:
            log.exception(f"[{cr_number}] FALHA no imposto {code} ({i}/{len(faltam)}). "
                          f"Ja inseridos e confirmados: {inseridos}. ABORTANDO.")
            _prog(on_progresso, code=code, fase="ERRO", i=i, n=len(faltam))
            try:
                driver.save_screenshot(os.path.join(
                    PRINTS_DIR, f"falha_{cr_number}_{code}_{datetime.now():%Y%m%d_%H%M%S}.png"))
            except Exception:
                pass
            raise
    return {"inseridos": inseridos, "pulados": pulados, "faltavam": faltam}


# ============================================================
# PERSISTENCIA (Save / Submit) - pre e pos-condicoes
# ============================================================
# 3o ponto da 7a revisao do Codex. Save e Submit sao as UNICAS acoes do projeto
# que gravam dado de verdade e nenhuma das duas foi exercida ate hoje
# (2026-09-11). A checagem generica do fim do finalize ("Change Request" + um
# dos 3 botoes) serve pra rodada SEM persistencia, mas e' fraca como
# pre-condicao de gravar: "Change Request" e' texto generico, innerText da raiz
# aparece ate' por baixo de modal, e "qualquer um dos 3" deixaria passar uma
# tela onde so' "Withdraw Draft" existe. Aqui a barra e' outra: identificar O
# botao, conferir que esta habilitado, e clicar NELE (por id) - nao "no primeiro
# elemento com esse texto".

# Preambulo compartilhado: quem enxerga a tela raiz (estado) e quem CLICA usam
# exatamente as mesmas regras. Se divergissem, a checagem aprovaria um botao e o
# clique acertaria outro - que e' o tipo de bug que so aparece gravando dado.
_JS_ACAO_RAIZ_PREAMBULO = r"""
const norm = (s) => (s || '').replace(/\u00ad/g, '').replace(/\s+/g, ' ').trim();

function marcadoDesabilitado(n) {
    if (!n || !n.getAttribute) return false;
    if ((n.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return true;
    if (n.hasAttribute && n.hasAttribute('disabled')) return true;
    const cls = (typeof n.className === 'string' ? n.className : '') || '';
    return /dsbl|disabled/i.test(cls);
}

// Dialogos/modais visiveis. Qualquer um = a tela precisa de decisao humana.
function coletarModais() {
    const out = [];
    for (const el of document.querySelectorAll('[role="dialog"], [role="alertdialog"], [aria-modal="true"]')) {
        const r = el.getBoundingClientRect();
        if (r.width < 40 || r.height < 20) continue;
        const st = window.getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') continue;
        out.push({id: el.id || '', texto: norm(el.innerText).slice(0, 300)});
    }
    return out;
}

// Mensagens da barra do WDA com severidade ESTRUTURAL (classe/role), nao por
// palavra: "Invalid withholding tax code" nao casa com nenhuma lista de
// padroes, mas vem numa .urMsgBarError. Texto entra so como informacao extra.
//
// DEDUPLICACAO ESTRUTURAL (12a revisao do Codex, 2026-09-23). Na sessao A o SAP
// publicou a confirmacao de sucesso DUAS vezes: uma no container com classe
// (sapLsFPMMessageArea -> 'info') e outra num [role=alert] SEM classe, que cai
// em 'desconhecido' e bloqueia. A regra estrutural funcionou como desenhada, e
// barrou a palavra "saved" depois de um Save que gravou.
// A saida NAO e' afrouxar 'desconhecido' - e' reconhecer que os dois nos sao a
// MESMA mensagem. Um no' sem classe so herda a severidade de outro quando:
//   (a) ha relacao ESTRUTURAL (um contem o outro, ou dividem o mesmo container
//       de mensagem do WDA) - "mesma tela" seria amplo demais: um container
//       informativo pode agregar o texto de varias mensagens e rebaixar um erro
//       desconhecido legitimo;
//   (b) o texto do menor e' NUCLEO do maior (prefixo com fronteira), nao
//       substring arbitraria;
//   (c) quem cede a severidade tem classe propria (identidade estrutural).
// Severidades em conflito: vence sempre a mais FORTE (erro > aviso >
// desconhecido > info). 'desconhecido' sem par estrutural continua bloqueando.
// Caminho ate o no', pra responder de vez ONDE o WDA publica cada mensagem.
// A hipotese inicial (o [role=alert] seria filho do container com classe) NAO
// bate com a sessao A: o innerText do container nao continha o texto do alert,
// logo ele e' uma live region separada. Sem o caminho real, a dedupe estrutural
// fica no escuro - entao ele passou a sair no log junto de cada alerta.
function caminhoDe(el) {
    const partes = [];
    let cur = el;
    for (let i = 0; i < 6 && cur && cur.nodeType === 1; i++) {
        let p = cur.tagName.toLowerCase();
        if (cur.id) p += '#' + cur.id;
        const c = (typeof cur.className === 'string' ? cur.className : '') || '';
        if (c) p += '.' + c.trim().split(/\s+/).slice(0, 2).join('.');
        const rl = cur.getAttribute && cur.getAttribute('role');
        if (rl) p += '[role=' + rl + ']';
        partes.unshift(p);
        cur = cur.parentElement;
    }
    return partes.join(' > ');
}

// Nos de mensagem - UMA definicao, usada pela classificacao e pelo dump de HTML.
const SEL_ALERTAS = '[role="alert"], [role="alertdialog"], [role="status"], '
                  + '[class*="MsgBar"], [class*="msgBar"], [class*="Message"]';

function coletarAlertas() {
    const FORCA = {erro: 3, aviso: 2, desconhecido: 1, info: 0};
    const sel = SEL_ALERTAS;

    // 1) COLETA TUDO primeiro. Nao deduplicar por texto aqui: dois nos com o
    // mesmo texto podem ter severidades diferentes, e cortar cedo deixava
    // prevalecer o primeiro - inclusive por cima de um irmao com classe de erro.
    const nos = [];
    for (const el of document.querySelectorAll(sel)) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        const t = norm(el.innerText);
        if (!t) continue;
        const cls = (typeof el.className === 'string' ? el.className : '') || '';
        const role = (el.getAttribute('role') || '').toLowerCase();
        let sev = 'info';
        if (/error|erro/i.test(cls)) sev = 'erro';
        else if (/warning|warn/i.test(cls)) sev = 'aviso';
        else if (role === 'alert' || role === 'alertdialog') sev = 'desconhecido';
        const rr = el.getBoundingClientRect();
        nos.push({el: el, texto: t, classe: cls, role: role, sev: sev,
                  id: el.id || '', caminho: caminhoDe(el),
                  x: Math.round(rr.left), y: Math.round(rr.top),
                  w: Math.round(rr.width), h: Math.round(rr.height)});
    }

    // tira pontuacao/espaco das pontas: o WDA prefixa a versao ARIA com ": "
    const nucleo = (s) => s.replace(/^[\s:;.,–—-]+/, '')
                           .replace(/[\s:;.,–—-]+$/, '');

    // NUCLEO, nao substring solta: o menor tem que comecar o maior, e terminar
    // numa fronteira (fim, pontuacao/espaco, ou inicio de outra palavra com
    // maiuscula - o WDA cola "Info." logo apos o texto).
    function ehNucleoDe(menor, maior) {
        const a = nucleo(menor), b = nucleo(maior);
        if (!a || a.length > b.length || b.indexOf(a) !== 0) return false;
        const resto = b.slice(a.length);
        return resto === '' || /^[\s:;.,–—-]/.test(resto) || /^[A-Z]/.test(resto);
    }

    const CONTAINER_MSG = '[class*="MessageArea"], [class*="MsgBar"], [class*="msgBar"]';
    function relacionados(a, b) {
        if (a.el.contains(b.el) || b.el.contains(a.el)) return true;
        const ca = a.el.closest(CONTAINER_MSG), cb = b.el.closest(CONTAINER_MSG);
        return !!(ca && cb && ca === cb);
    }

    // ECO ARIA DO WDA (2026-09-28, HTML real do Save da CR 90000003). O
    // [role=alert] sem classe de 23/09 e':
    //   <span id="sapur-aria-alert" role="alert" aria-hidden="true">
    //     <span>: Change request 90000003 saved</span></span>
    // em html > body > div#sapur-aria > div#sapur-aria-dynamic-texts, fora da
    // tela (y=-70003). E' a copia que o renderizador publica pro LEITOR DE TELA
    // da mensagem que acabou de mostrar na area visivel - por isso nao tem
    // relacao estrutural com ela e a dedupe acima nunca disparava.
    // Regra ESPECIFICA (Codex, 36a entrada - nao "permitir toda ressalva"): o
    // eco so herda de UMA configuracao exata, e todas as condicoes valem juntas:
    //   (a) e' o no' global do WDA: id sapur-aria-alert, aria-hidden=true, pai
    //       #sapur-aria-dynamic-texts, avo #sapur-aria;
    //   (b) o par e' a area de mensagens do FPM (sapLsFPMMessageArea);
    //   (c) o texto da area e' EXATAMENTE o nucleo do eco + "Info." (o rotulo
    //       acessivel do icone), opcionalmente + "Display Message Log" - nada
    //       mais. A classe da area e' a mesma pra erro e pra info (o log so
    //       trouxe 2500 chars do HTML, antes do icone), entao e' este rotulo que
    //       separa as duas. Ele RESTRINGE, nunca classifica: "Error.", texto a
    //       mais ou outra mensagem no eco deixam o eco 'desconhecido', que
    //       continua bloqueando como antes. Errar aqui custa uma ressalva, nunca
    //       um erro engolido.
    function ehEcoAriaDe(eco, msg) {
        const el = eco.el, pai = el.parentElement, avo = pai && pai.parentElement;
        if (el.id !== 'sapur-aria-alert'
            || (el.getAttribute('aria-hidden') || '').toLowerCase() !== 'true'
            || !pai || pai.id !== 'sapur-aria-dynamic-texts'
            || !avo || avo.id !== 'sapur-aria') return false;
        if (!/(^|\s)sapLsFPMMessageArea(\s|$)/.test(msg.classe) || msg.sev !== 'info') return false;
        const a = nucleo(eco.texto);
        if (!a || msg.texto.indexOf(a) !== 0) return false;
        return /^Info\.(\s*Display Message Log)?$/.test(msg.texto.slice(a.length).trim());
    }

    // 2) severidade EFETIVA por no'
    for (const n of nos) {
        n.sev_efetiva = n.sev;
        n.duplicado_de = null;
        if (n.sev !== 'desconhecido' || n.classe) continue;   // so no' sem classe herda
        for (const m of nos) {
            if (m === n || !m.classe) continue;               // so cede quem tem classe
            const par = (relacionados(n, m) && ehNucleoDe(n.texto, m.texto))
                     || ehEcoAriaDe(n, m);
            if (!par) continue;
            if (n.duplicado_de === null || FORCA[m.sev] > FORCA[n.sev_efetiva]) {
                n.sev_efetiva = m.sev;
                n.duplicado_de = m.texto.slice(0, 120);
            }
        }
    }

    // 3) agrupa por nucleo do texto e emite a severidade MAIS FORTE do grupo
    const porTexto = {}, ordem = [];
    for (const n of nos) {
        const k = nucleo(n.texto);
        if (!porTexto[k]) { porTexto[k] = n; ordem.push(k); continue; }
        const atual = porTexto[k];
        if (FORCA[n.sev_efetiva] > FORCA[atual.sev_efetiva]) porTexto[k] = n;
    }
    const out = [];
    for (const k of ordem) {
        const n = porTexto[k];
        out.push({
            texto: n.texto.slice(0, 300),
            classe: n.classe.slice(0, 120),
            role: n.role,
            severidade: n.sev_efetiva,
            severidade_original: n.sev,
            duplicado_de: n.duplicado_de,
            id: n.id,
            caminho: n.caminho.slice(0, 300),
            pos: [n.x, n.y, n.w, n.h]
        });
    }
    return out;
}

// 1 entrada por BOTAO REAL. O dedupe e' pela identidade do no `host` (o
// ancestral com lsevents/role=button que recebe o clique - gotcha 5), nao por
// prefixo de id: dois controles distintos podem compartilhar o prefixo antes do
// hifen e colapsariam num so (8a revisao do Codex).
// O ancestral clicavel DESTE elemento - nao qualquer ancestral com lsevents.
// Convencao do WDA (gotcha 6): os varios nos de um botao compartilham o id
// tecnico e diferem so no sufixo (-cnt, -caption, -r, ...). Se o id do
// ancestral nao for prefixo do id do elemento, subimos demais: foi o que
// aconteceu na rodada de 14/09, em que 'WD6012-r' (no de leitura, sem lsevents
// proprio) casou com a raiz da janela 'sapwd_main_window_root_' e virou um
// candidato separado do botao 'WD6012' de verdade.
// O ancestral MAIS PROXIMO entre lsevents e role=button (24/09). Antes o
// lsevents tinha prioridade mesmo longe: num botao desabilitado sem lsevents,
// o '-cnt' pulava o proprio botao (role=button, logo acima) e ia ate a raiz
// da janela - e virava um candidato a parte, quebrando "exatamente 1 Save".
function hostClicavel(el) {
    const cand = el.closest('[lsevents], [role="button"]');
    if (!cand || cand === el) return el;
    const hid = String(cand.id || ''), eid = String(el.id || '');
    if (hid && eid) return eid.indexOf(hid) === 0 ? cand : el;
    // sem id pra comparar: so aceita se for ancestral PROXIMO (<= 3 niveis)
    let n = el.parentElement, niveis = 0;
    while (n && n !== cand && niveis < 3) { n = n.parentElement; niveis++; }
    return n === cand ? cand : el;
}

function pareceBotaoVisivel(el) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    if (r.width > 220 || r.height > 60) return false;      // wrapper gigante, nao e' botao
    if (r.top < 0 || r.left < 0) return false;             // rolado pra fora da tela
    const st = window.getComputedStyle(el);
    return !(st.display === 'none' || st.visibility === 'hidden');
}

// Gotcha 19 (ensaio de 24/09): cada botao da barra da raiz vem dentro de um
// wrapper de toolbar 'X-r' (span.lsToolbar--item, 41x32 em volta do botao 'X'
// de 41x26), com o MESMO texto e SEM lsevents. Como o wrapper e' ANCESTRAL do
// botao, hostClicavel() - que so sobe - nunca o junta, e ele virava um 2o
// candidato: 'exatamente 1 Save' falhava antes do Submit. Canoniza SO o sufixo
// exato '-r', e so quando (Codex, 36a entrada): o elemento-base existe; e' o
// controle real (lsevents, role=button ou nativo) e visivel como botao; tem o
// mesmo texto; e o '-r' nao tem Press proprio. Qualquer outro caso continua
// candidato separado - fail-closed nas duas contagens.
function baseDoSufixoR(el, alvo) {
    const m = /^(.+)-r$/.exec(String(el.id || ''));
    if (!m) return null;
    const base = document.getElementById(m[1]);
    if (!base || base === el) return null;
    if (/press/i.test((el.getAttribute && el.getAttribute('lsevents')) || '')) return null;
    if (norm(base.innerText || base.value || '').toLowerCase() !== alvo) return null;
    const ehControle = base.hasAttribute('lsevents') || base.getAttribute('role') === 'button'
                       || /^(button|input)$/i.test(base.tagName);
    if (!ehControle || !pareceBotaoVisivel(base)) return null;
    return base;
}

function candidatosAcao(alvo) {
    const hosts = [], cands = [];
    for (const el of document.querySelectorAll('a, span, div, button, input[type="button"], input[type="submit"]')) {
        const t = norm(el.innerText || el.value || '');
        if (t.toLowerCase() !== alvo) continue;
        if (!pareceBotaoVisivel(el)) continue;
        const r = el.getBoundingClientRect();

        let host = hostClicavel(el);
        let canonizadoDe = '';
        if (host === el) {
            const base = baseDoSufixoR(el, alvo);
            if (base) { host = hostClicavel(base); canonizadoDe = el.id; }
        }
        const ev = (host.getAttribute && host.getAttribute('lsevents')) || '';
        const temPress = /press/i.test(ev);
        // No canonizado, o estado vem SO do controle real: o wrapper '-r' nao
        // tem acao propria, e uma marca dele nao pode contradizer o botao.
        const nativo = canonizadoDe ? /^(button|input)$/i.test(host.tagName)
            : (/^(button|input)$/i.test(el.tagName) || /^(button|input)$/i.test(host.tagName));
        const desab = canonizadoDe ? marcadoDesabilitado(host)
            : (marcadoDesabilitado(el) || marcadoDesabilitado(host));
        // WDA: exigir Press POSITIVAMENTE. O gotcha 6 documenta que botao
        // desabilitado do WDA continua batendo no seletor por texto, e ele pode
        // nao ter nenhuma das marcas de "disabled" - o que sobra e' o handler.
        const habilitado = nativo ? !desab : (temPress && !desab);
        const c = {
            _el: host, id: el.id || '', host_id: host.id || '',
            tag: el.tagName.toLowerCase(), host_tag: host.tagName.toLowerCase(),
            texto: t, w: Math.round(r.width), h: Math.round(r.height),
            x: Math.round(r.left), y: Math.round(r.top),
            classe: ((typeof host.className === 'string' ? host.className : '') || '').slice(0, 90),
            tabindex: (host.getAttribute && host.getAttribute('tabindex')) || '',
            aria_disabled: (host.getAttribute && host.getAttribute('aria-disabled')) || '',
            lsevents: ev.slice(0, 60),
            tem_press: temPress, nativo: nativo, desabilitado: desab, habilitado: habilitado,
            canonizado_de: canonizadoDe
        };
        const k = hosts.indexOf(host);
        if (k === -1) { c.nos = [c.id]; hosts.push(host); cands.push(c); continue; }
        // `nos` = todos os elementos que viraram ESTE botao, pra auditoria no log.
        const nos = cands[k].nos.concat([c.id]);
        const canon = cands[k].canonizado_de || c.canonizado_de;
        if (c.tem_press && !cands[k].tem_press) { cands[k] = c; }
        cands[k].nos = nos;
        cands[k].canonizado_de = canon;
    }
    return cands;
}

function semDomRef(c) { const o = {}; for (const k in c) { if (k !== '_el') o[k] = c[k]; } return o; }
"""

# arg0 = texto exato da acao ("Save"/"Submit"). READ-ONLY.
_JS_ESTADO_ACAO_RAIZ = _JS_ACAO_RAIZ_PREAMBULO + r"""
return {
    candidatos: candidatosAcao(norm(arguments[0]).toLowerCase()).map(semDomRef),
    modais: coletarModais(),
    alertas: coletarAlertas()
};
"""

# arg0 = acao, arg1 = numero da CR. REVALIDA E CLICA NA MESMA EXECUCAO - nao
# existe janela entre conferir e clicar (TOCTOU do ponto 1 da 8a revisao: a
# pre-condicao rodava antes de um pause humano e o clique vinha depois).
_JS_CLICAR_ACAO_RAIZ = _JS_ACAO_RAIZ_PREAMBULO + r"""
const alvo = norm(arguments[0]).toLowerCase();
const cr = String(arguments[1] || '');
const corpo = norm(document.body.innerText || '');
if (!corpo) return {erro: 'tela vazia no momento do clique'};
if (cr && corpo.indexOf(cr) === -1) return {erro: 'a CR ' + cr + ' nao esta na tela no momento do clique'};

const modais = coletarModais();
if (modais.length) return {erro: 'modal bloqueante no momento do clique: '
    + JSON.stringify(modais).slice(0, 300)};

// arguments[2] = severidades bloqueantes, vindas de _severidades_bloqueantes().
// NAO repetir a regra aqui: ela divergiria do Python em silencio.
const sevBloq = arguments[2] || ['erro', 'desconhecido'];
const bloq = coletarAlertas().filter(a => sevBloq.indexOf(a.severidade) !== -1);
if (bloq.length) return {erro: 'alerta bloqueante no momento do clique: '
    + JSON.stringify(bloq).slice(0, 300)};

const cands = candidatosAcao(alvo).filter(c => c.habilitado);
if (cands.length !== 1) return {erro: cands.length + ' botao(oes) ' + JSON.stringify(arguments[0])
    + ' habilitado(s) no momento do clique (esperado 1): '
    + JSON.stringify(candidatosAcao(alvo).map(semDomRef)).slice(0, 400)};

const c = cands[0];
c._el.scrollIntoView({block: 'center'});
c._el.click();
return {ok: true, clicado: semDomRef(c)};
"""

# Clique de Submit da sessao B. Alem das travas genericas da acao raiz, exige
# atomicamente que Save continue EXISTINDO e DESABILITADO no exato comando que
# clica Submit. A conferencia e o checkpoint humano acontecem antes, portanto
# sem esta revalidacao haveria uma janela TOCTOU entre o atestado e o clique.
# arg0 = CR; arg1 = severidades bloqueantes.
_JS_CLICAR_SUBMIT_VALIDADO = _JS_ACAO_RAIZ_PREAMBULO + r"""
const cr = String(arguments[0] || '');
const sevBloq = arguments[1] || ['erro', 'desconhecido'];
const corpo = norm(document.body.innerText || '');
if (!corpo) return {erro: 'tela vazia no momento do Submit'};
if (!cr || corpo.indexOf(cr) === -1) {
    return {erro: 'a CR ' + cr + ' nao esta na tela no momento do Submit'};
}

const modais = coletarModais();
if (modais.length) return {erro: 'modal bloqueante no momento do Submit: '
    + JSON.stringify(modais).slice(0, 300)};
const bloq = coletarAlertas().filter(a => sevBloq.indexOf(a.severidade) !== -1);
if (bloq.length) return {erro: 'alerta bloqueante no momento do Submit: '
    + JSON.stringify(bloq).slice(0, 500)};

const saves = candidatosAcao('save');
const savesHab = saves.filter(c => c.habilitado);
if (saves.length !== 1 || savesHab.length !== 0) {
    return {erro: 'pre-condicao do Submit falhou: esperava exatamente 1 Save '
        + 'existente e DESABILITADO; candidatos='
        + JSON.stringify(saves.map(semDomRef)).slice(0, 500)};
}

const submits = candidatosAcao('submit');
const submitsHab = submits.filter(c => c.habilitado);
if (submitsHab.length !== 1) {
    return {erro: 'esperava exatamente 1 Submit habilitado, achei '
        + submitsHab.length + ' (de ' + submits.length + '): '
        + JSON.stringify(submits.map(semDomRef)).slice(0, 500)};
}

const c = submitsHab[0];
c._el.scrollIntoView({block: 'center'});
c._el.click();
return {ok: true, clicado: semDomRef(c), save: semDomRef(saves[0])};
"""

# READ-ONLY (Codex, 36a entrada): o HTML REAL de cada no' de mensagem depois de
# persistir. Falta decidir, com dado, se o [role=alert] sem classe da mensagem
# "saved" de 23/09 pode herdar a severidade do container: a dedupe estrutural so
# junta com relacao estrutural, e os textos sugerem uma live region separada.
# Sem este HTML (ancestrais, aria-live), a regra seria escrita no escuro.
_JS_DUMP_ALERTAS_HTML = _JS_ACAO_RAIZ_PREAMBULO + r"""
function rotulo(p) {
    let s = p.tagName.toLowerCase() + (p.id ? '#' + p.id : '');
    const c = (typeof p.className === 'string' ? p.className : '') || '';
    if (c.trim()) s += '.' + c.trim().split(/\s+/).slice(0, 3).join('.');
    for (const a of ['role', 'aria-live', 'aria-atomic']) {
        const v = p.getAttribute && p.getAttribute(a);
        if (v) s += '[' + a + '=' + v + ']';
    }
    return s;
}
const out = [];
for (const el of document.querySelectorAll(SEL_ALERTAS)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || !norm(el.innerText)) continue;
    const ancestrais = [];
    for (let p = el.parentElement, i = 0; p && i < 12; p = p.parentElement, i++) {
        ancestrais.push(rotulo(p));
    }
    // 28/09: o outerHTML da area do FPM passa de 2500 chars so' de tabelas de
    // layout, e o icone que distingue Info/Error ficava de fora. As classes dos
    // descendentes cabem em pouco espaco e trazem esse marcador.
    const classes = new Set();
    for (const f of el.querySelectorAll('[class]')) {
        const c = typeof f.className === 'string' ? f.className : '';
        for (const k of c.trim().split(/\s+/)) if (k) classes.add(k);
        if (classes.size >= 150) break;
    }
    out.push({no: rotulo(el), texto: norm(el.innerText).slice(0, 200),
              pos: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
              ancestrais: ancestrais, outer: el.outerHTML.slice(0, 2500),
              classes_descendentes: Array.from(classes)});
    if (out.length >= 12) break;
}
return out;
"""


def _registrar_html_alertas(driver, cr_number, acao):
    """Loga o HTML real das mensagens depois de persistir. Evidencia, nao
    decisao: NUNCA levanta e nunca muda veredito."""
    try:
        nos = _execute_script_critico(driver, _JS_DUMP_ALERTAS_HTML) or []
    except Exception as e:
        log.warning(f"[{cr_number}] pos-{acao}: nao consegui dumpar o HTML das mensagens "
                    f"({type(e).__name__}: {e})")
        return []
    for i, n in enumerate(nos, start=1):
        log.info(f"[{cr_number}] pos-{acao} HTML da mensagem {i}/{len(nos)}: {n!r}")
    return nos


# Severidades que BLOQUEIAM persistencia. 'desconhecido' = [role=alert] sem
# classe reconhecida: nao da pra afirmar que e' benigno, e aqui o custo de
# recusar a toa e' um minuto, o de gravar errado e' uma CR real.
_SEVERIDADES_BLOQUEANTES = ("erro", "desconhecido")

# P1 da 10a revisao do Codex (2026-09-22): no PRIMEIRO Save/Submit, tratar
# tambem 'aviso' como bloqueante. coletarAlertas() ja sabe classificar aviso,
# mas nem o relatorio nem o clique atomico olhavam pra ele. Custo de recusar a
# toa: um minuto e uma leitura de tela. Custo de gravar por cima de um aviso
# que ninguem leu: uma CR real. Depois da 1a rodada, se aparecer um aviso
# benigno RECORRENTE, vira allowlist daquele texto - nao um desligar geral.
BLOQUEAR_AVISOS_AO_PERSISTIR = True


def _severidades_bloqueantes():
    """Fonte UNICA da regra de severidade. O JS atomico recebe o resultado
    desta funcao como argumento (nao tem copia da regra), entao os dois nao
    podem divergir - que era o risco apontado pelo Codex."""
    if BLOQUEAR_AVISOS_AO_PERSISTIR:
        return _SEVERIDADES_BLOQUEANTES + ("aviso",)
    return _SEVERIDADES_BLOQUEANTES


def _estado_acao_raiz(driver, texto):
    """READ-ONLY: candidatos a botao `texto` (1 por botao real), modais e
    alertas com severidade. Nao clica em nada."""
    # critico: um alerta aqui nao pode ser aceito em silencio - ele pode ser
    # justamente o dialogo que a acao de persistencia abriu.
    r = _execute_script_critico(driver, _JS_ESTADO_ACAO_RAIZ, texto)
    if not isinstance(r, dict):
        return {"candidatos": [], "modais": [], "alertas": [],
                "erro": f"retorno {type(r).__name__}"}
    return r


def dump_acoes_raiz(driver, cr_number=None):
    """
    READ-ONLY: dump COMPLETO dos candidatos a Save / Submit / Withdraw Draft na
    tela raiz da CR, sem clicar em nada. Pensado pra ser chamado depois dos 2
    "Done" numa rodada SEM persistencia (item 6.3 da 8a revisao do Codex): e' o
    que responde, numa janela so, o dedupe, o marcador de "desabilitado" e se o
    numero da CR aparece mesmo na tela - as tres duvidas que hoje seguram o Save.
    """
    saida = {"cr_na_tela": None, "acoes": {}, "assinatura": {}}
    if cr_number is not None:
        txt = _safe_execute_script(driver, "return document.body.innerText || '';") or ""
        saida["cr_na_tela"] = str(cr_number) in txt
        # Candidatos a ASSINATURA ALTERNATIVA da CR. Motivo (14/09): nos dois
        # screenshots da tela raiz de 10/09 o numero da CR nao aparece - o
        # titulo e' "Organization: $1000003, ...". Se `cr_na_tela` vier False
        # numa rodada real, e' daqui que sai o identificador substituto, sem
        # gastar outra janela de CR destravada pra descobrir.
        saida["assinatura"] = _safe_execute_script(driver, r"""
const norm = (s) => (s || '').replace(/\u00ad/g, '').replace(/\s+/g, ' ').trim();
const alvo = String(arguments[0] || '');
const onde = [];
if (alvo) {
    for (const el of document.querySelectorAll('*')) {
        if (el.children.length) continue;                 // so folhas
        const t = norm(el.innerText);
        if (!t || t.indexOf(alvo) === -1) continue;
        const r = el.getBoundingClientRect();
        onde.push({id: el.id || '', tag: el.tagName.toLowerCase(),
                   texto: t.slice(0, 120), visivel: r.width > 0 && r.height > 0});
        if (onde.length >= 6) break;
    }
}
return {
    titulo_documento: norm(document.title).slice(0, 200),
    topo_da_tela: norm(document.body.innerText || '').slice(0, 400),
    onde_aparece_a_cr: onde,
    tem_iframes: document.querySelectorAll('iframe').length
};
""", str(cr_number)) or {}
    for acao in ("Save", "Submit", "Withdraw Draft"):
        est = _estado_acao_raiz(driver, acao)
        saida["acoes"][acao] = est
        log.info(f"[raiz] {acao}: {len(est.get('candidatos') or [])} candidato(s)")
        for c in est.get("candidatos") or []:
            log.info(f"[raiz]   {c!r}")
        if est.get("modais"):
            log.info(f"[raiz]   modais={est['modais']!r}")
        if est.get("alertas"):
            log.info(f"[raiz]   alertas={est['alertas']!r}")
    log.info(f"[raiz] numero da CR visivel na tela: {saida['cr_na_tela']}")
    return saida


class AntesDoClique(RuntimeError):
    """A persistencia foi RECUSADA antes de qualquer clique (relatorio previo ou
    o JS atomico recusou sem clicar). Nada foi gravado. Distinguir disso o erro
    depois do clique e' o que permite relatar "nada gravado" em vez de "o Save
    pode ter gravado" (revisao de 29/09)."""


def _relatar_acao_raiz(driver, cr_number, acao):
    """
    Confere e LOGA o estado da tela antes de persistir. Levanta se ja da' pra
    afirmar que nao dá: CR errada/ausente, modal, alerta bloqueante, ou != 1
    botao habilitado.

    Isto NAO autoriza o clique - quem autoriza e' _clicar_acao_raiz, que refaz
    as mesmas checagens NA MESMA EXECUCAO do clique. Este relatorio existe pra
    falhar com mensagem legivel ANTES de pedir confirmacao ao operador.
    """
    # critico (gotcha 14): esta leitura abre a janela de persistencia. Se ja
    # houver um dialogo na tela, ele NAO pode ser aceito aqui - e' informacao
    # sobre o estado da CR, nao ruido a dispensar.
    txt = _execute_script_critico(driver, "return document.body.innerText || '';") or ""
    if str(cr_number) not in txt:
        raise AntesDoClique(
            f"{acao}: nao achei o numero {cr_number} na tela - nao clico em '{acao}' sem "
            f"confirmar QUAL CR esta aberta. Fail closed. tela: {txt[:200]!r}")

    est = _estado_acao_raiz(driver, acao)
    if est.get("erro"):
        raise AntesDoClique(f"{acao}: nao consegui ler o estado da tela ({est['erro']}). Fail closed.")
    if est.get("modais"):
        raise AntesDoClique(f"{acao}: dialogo/modal bloqueante na tela: {est['modais']!r}")
    bloq = [a for a in (est.get("alertas") or []) if a.get("severidade") in _severidades_bloqueantes()]
    if bloq:
        raise AntesDoClique(f"{acao}: mensagem bloqueante na tela ANTES do clique: {bloq!r}")
    for a in est.get("alertas") or []:
        log.info(f"[{cr_number}] alerta na tela antes do {acao}: {a!r}")

    cands = est.get("candidatos") or []
    habilitados = [c for c in cands if c.get("habilitado")]
    if len(habilitados) != 1:
        raise AntesDoClique(
            f"{acao}: esperava exatamente 1 botao '{acao}' habilitado, achei {len(habilitados)} "
            f"(de {len(cands)} candidato(s)). Fail closed. candidatos={cands!r}")
    log.info(f"[{cr_number}] tela pronta pro {acao}: {habilitados[0]!r}")
    return habilitados[0]


def _clicar_acao_raiz(driver, cr_number, acao):
    """
    Revalida (CR na tela, sem modal, sem alerta bloqueante, exatamente 1 botao
    habilitado) e clica - TUDO na mesma execucao de JS. Nao existe janela entre
    conferir e clicar.
    """
    r = _execute_script_critico(driver, _JS_CLICAR_ACAO_RAIZ, acao, str(cr_number),
                                list(_severidades_bloqueantes()))
    if isinstance(r, dict) and not r.get("ok") and r.get("erro"):
        raise AntesDoClique(f"{acao}: clique recusado ANTES de clicar - {r['erro']!r}")
    if not isinstance(r, dict) or not r.get("ok"):
        # Retorno vazio NAO e' recusa: um confirm() aberto pelo proprio clique faz
        # o execute devolver None (medido no Delete, caso 36). Pode ter clicado.
        # O portao critico transforma um dialogo aberto na mensagem certa.
        _execute_script_critico(driver, "return true;")
        raise RuntimeError(f"{acao}: resultado do clique ilegivel ({r!r}) - PODE ter clicado.")
    log.info(f"[{cr_number}] {acao} clicado: {r.get('clicado')!r}")
    return r.get("clicado")


def _evidencia_pos_clique(driver, cr_number, acao):
    """
    Parte COMUM de qualquer pos-condicao de persistencia: espera a tela,
    fotografa, le o estado e aborta so no que impede ATE MESMO diagnosticar
    (timeout, tela ilegivel, tela vazia). Devolve (estado, texto_da_tela).

    O que NAO esta aqui, de proposito: o julgamento. Modal, alerta e estado do
    botao voltam como EVIDENCIA, pra quem chamou decidir com tudo na mao - ver
    _poscondicao_save e _poscondicao_submit.

    FAIL-CLOSED no timeout (ponto 1e da 8a revisao do Codex): o cenario que isso
    mata e' o Save ainda processando atras do blindlayer, com a tela ANTIGA
    mostrando "Submit" e nenhum texto de erro - antes isso passava por
    "concluido" e podia submeter.
    """
    try:
        # critico=True: o intervalo entre o clique e a confirmacao e' exatamente
        # onde um alert/confirm tardio do WDA cairia (gotcha 14).
        wait_for_wda_ready(driver, timeout=45, critico=True)
    except TimeoutException:
        try:
            driver.save_screenshot(os.path.join(
                PRINTS_DIR, f"persist_timeout_{acao.lower()}_{cr_number}_{datetime.now():%Y%m%d_%H%M%S}.png"))
        except Exception:
            pass
        raise RuntimeError(
            f"{acao}: a tela NAO estabilizou em 45s depois do clique. Nao da' pra afirmar "
            f"se {acao} concluiu - pode estar processando ainda. ABORTANDO (nao sigo pro "
            f"passo seguinte). Conferir na tela/print antes de qualquer nova tentativa.")
    try:
        driver.save_screenshot(os.path.join(
            PRINTS_DIR, f"persist_{acao.lower()}_{cr_number}_{datetime.now():%Y%m%d_%H%M%S}.png"))
    except Exception:
        pass

    est = _estado_acao_raiz(driver, acao)
    if est.get("erro"):
        raise RuntimeError(f"{acao}: tela ilegivel depois do clique ({est['erro']}). Fail closed.")
    txt = _execute_script_critico(driver, "return document.body.innerText || '';") or ""
    if not txt:
        raise RuntimeError(f"{acao}: tela vazia depois do clique. Fail closed.")
    log.debug(f"[{cr_number}] pos-{acao}: tela[:400]={txt[:400]!r}")
    return est, txt


def _poscondicao_save(driver, cr_number, estado_antes=None):
    """
    Depois do clique em Save. NAO desfaz nada (o clique ja aconteceu) -
    serve pra decidir, com TODAS as evidencias na mao, se da' pra seguir pro
    passo seguinte, e pra deixar registro do que a tela mostrava.

    DECISAO CONJUNTA (12a revisao do Codex, 2026-09-23). Antes isto era uma
    sequencia de `raise` e parava no primeiro sinal negativo - foi assim que a
    sessao A terminou em "Falha" depois de um Save que GRAVOU: abortou no alerta
    e nunca chegou a olhar o botao, que estava ali na tela, desabilitado,
    dizendo que tinha dado certo. Perder essa evidencia e' pior que o bug.

    Agora coleta tudo e so entao classifica:

      | evidencia                                   | veredito      | segue? |
      |---------------------------------------------|---------------|--------|
      | botao desabilitado, sem bloqueante           | confirmado    | sim    |
      | botao desabilitado + desconhecido SEM par    | ressalva      | NAO    |
      | botao desabilitado + erro/aviso/modal        | problema      | NAO    |
      | botao habilitado / sumiu / tela ilegivel     | indeterminado | NAO    |

    O sinal positivo MELHORA o diagnostico, mas nunca neutraliza sozinho uma
    evidencia negativa - e' isso que mantem o fail-closed onde ele importa:
    nenhum estado com ressalva autoriza o passo irreversivel seguinte.

    Devolve o veredito em `confirmado` e `ressalva`; LEVANTA em `problema` e
    `indeterminado`, que sao os casos em que nem o diagnostico e' confiavel.

    CRITERIO POSITIVO do botao (achado em 14/09 nos prints de 10/09, CONFIRMADO
    na sessao A de 23/09): o "Save" fica CINZA quando nao ha alteracao pendente
    e HABILITADO quando ha. Depois de um Save que pegou, ele volta a desabilitar
    - foi exatamente o que o print `persist_save_90000001_20260923_122301.png`
    mostrou. Este sinal vale pro SAVE; um Submit pode navegar ou sumir com os
    botoes, e ai ele nao se aplica (ver gotcha 15).
    """
    acao = "Save"
    est, txt = _evidencia_pos_clique(driver, cr_number, acao)

    # --- coleta TUDO antes de decidir ---
    modais = est.get("modais") or []
    alertas = est.get("alertas") or []
    bloqueantes = [a for a in alertas if a.get("severidade") in _severidades_bloqueantes()]
    # 'desconhecido' que nao achou par estrutural: nao da' pra afirmar que e'
    # erro, nem que e' benigno. E' exatamente o caso da mensagem de sucesso da
    # sessao A - e tambem seria o de um erro que o WDA nao classificou.
    desconhecidos = [a for a in bloqueantes
                     if a.get("severidade") == "desconhecido" and not a.get("duplicado_de")]
    confirmados_ruins = [a for a in bloqueantes if a not in desconhecidos]

    cr_na_tela = str(cr_number) in txt
    candidatos = est.get("candidatos") or []
    habilitados = [c for c in candidatos if c.get("habilitado")]
    antes_habilitado = bool(estado_antes and estado_antes.get("habilitado"))
    botao_desabilitou = bool(candidatos) and not habilitados

    log.info(f"[{cr_number}] pos-{acao}: candidatos={len(candidatos)} habilitados={len(habilitados)} "
             f"(antes do clique estava habilitado={antes_habilitado}) cr_na_tela={cr_na_tela}")
    for a in alertas:
        log.info(f"[{cr_number}] pos-{acao} alerta: sev={a.get('severidade')!r} "
                 f"(original={a.get('severidade_original')!r}, duplicado_de="
                 f"{(a.get('duplicado_de') or '')[:60]!r}) id={a.get('id')!r} pos={a.get('pos')!r} "
                 f"caminho={a.get('caminho')!r} texto={a.get('texto', '')[:120]!r}")
    if alertas:
        _registrar_html_alertas(driver, cr_number, acao)

    # --- classifica ---
    motivos = []
    if not cr_na_tela:
        motivos.append(f"a CR {cr_number} nao esta mais na tela")
    if antes_habilitado and habilitados:
        motivos.append(f"o botao '{acao}' continua HABILITADO (alteracao ainda pendente)")
    if antes_habilitado and not candidatos:
        motivos.append(f"o botao '{acao}' SUMIU da tela (nem habilitado, nem desabilitado)")

    if motivos:
        raise RuntimeError(
            f"{acao}: resultado INDETERMINADO - {'; '.join(motivos)}. Nao da' pra afirmar o "
            f"que foi gravado. ABORTANDO pra conferencia manual (print salvo). "
            f"tela: {txt[:200]!r}")

    if modais or confirmados_ruins:
        raise RuntimeError(
            f"{acao}: PROBLEMA CONFIRMADO depois do clique. O dado PODE ter sido gravado "
            f"(o botao {'desabilitou' if botao_desabilitou else 'nao desabilitou'}), entao "
            f"NAO repita cegamente - confira a CR antes. modais={modais!r} "
            f"mensagens={confirmados_ruins!r}")

    if desconhecidos:
        log.warning(
            f"[{cr_number}] {acao} com RESSALVA: o botao indica que gravou, mas ha mensagem "
            f"que nao da' pra classificar. NAO e' falha e NAO deve ser repetido; tambem NAO "
            f"autoriza o passo seguinte. Confira a CR e o print antes de continuar. "
            f"mensagens={desconhecidos!r}")
        return {"veredito": "ressalva", "acao": acao, "botao_desabilitou": botao_desabilitou,
                "mensagens": desconhecidos, "alertas": alertas}

    if antes_habilitado and botao_desabilitou:
        log.info(f"[{cr_number}] {acao}: botao voltou a ficar DESABILITADO - sinal de que "
                 f"nao ha mais alteracao pendente.")
    log.info(f"[{cr_number}] {acao} CONFIRMADO (sem modal, sem mensagem bloqueante).")

    # Gotcha 7: o lock "so libera com Save, Submit, Withdraw Draft ou Undo".
    # Confirmado na sessao A. Fica logado pra continuar sendo verificado.
    ainda_travada = "locked by" in txt.lower()
    log.info(f"[{cr_number}] apos o {acao}, a CR aparece travada? {ainda_travada} "
             f"(gotcha 7 preve DESTRAVADA)")
    return {"veredito": "confirmado", "acao": acao, "botao_desabilitou": botao_desabilitou,
            "ainda_travada": ainda_travada, "alertas": alertas}


def _estado_aceita_submit(texto_normalizado, cr_number):
    """True se status e work item sao de uma CR em edicao pelo solicitante,
    cada um com o SEU par:
      - CR nova: "Changes to Be Executed" + "New Change Request";
      - CR devolvida pela aprovacao (print do negocio, 29/09 14:19): "To be
        Revised" + "Process Change Request [<esta CR>]".
    Qualquer outra combinacao (workflow em andamento, pares trocados, numero de
    outra CR no work item) e' recusada. `texto_normalizado`: innerText com os
    espacos colapsados (o WDA cola o rotulo "Status" depois do valor).
    """
    t = texto_normalizado
    if ("Status Changes to Be ExecutedStatus" in t
            and "Current Work Item New Change Request" in t):
        return True
    m = re.search(r"Current Work Item Process Change Request(?: (\d+))?", t)
    return bool("Status To be RevisedStatus" in t and m
                and m.group(1) in (None, str(cr_number)))


def _eh_submit_parou_na_checagem(cr_number, estado, texto_tela):
    """Reconhece o estado do 1o Submit de 28/09 (gotcha 20) por ESTRUTURA.

    Isto NAO e' sucesso e NAO autoriza outro clique. So separa "o clique rodou a
    checagem e a CR ficou onde estava" de um efeito desconhecido. Sinais, todos
    juntos: sem modal; CR na tela; status e work item ainda os de antes do
    Submit; exatamente 1 Submit, habilitado; a area do FPM abrindo com "No
    errors found" e so' rotulos "Info." (o rotulo acessivel do icone de cada
    mensagem); fora dela, no maximo o eco ARIA global. O texto das mensagens
    em si varia por fornecedor (28/09: CNPJ duplicado, "BR3") e fica de fora.
    Fixture: log_impostos_20260928_152446.txt.
    """
    if not isinstance(estado, dict) or estado.get("modais"):
        return False
    texto = " ".join(str(texto_tela or "").split())
    if str(cr_number) not in texto or not _estado_aceita_submit(texto, cr_number):
        return False

    submits = estado.get("candidatos") or []
    if not (len(submits) == 1
            and submits[0].get("habilitado") is True
            and submits[0].get("tem_press") is True):
        return False

    areas, ecos, outros = [], [], []
    for a in estado.get("alertas") or []:
        if ("sapLsFPMMessageArea" in (a.get("classe") or "").split()
                and a.get("role") == "presentation"
                and a.get("severidade") == "info"):
            areas.append(a)
        elif (a.get("id") == "sapur-aria-alert" and a.get("role") == "alert"
              and "#sapur-aria-dynamic-texts" in (a.get("caminho") or "")
              and (re.fullmatch(r"\d+ messages?", a.get("texto") or "")
                   or a.get("duplicado_de"))):
            ecos.append(a)
        else:
            outros.append(a)
    if len(areas) != 1 or len(ecos) > 1 or outros:
        return False
    texto_area = " ".join((areas[0].get("texto") or "").split())
    if not texto_area.startswith("No errors foundInfo."):
        return False
    rotulos = re.findall(r"(Info|Warning|Error|Success)\.", texto_area)
    return bool(rotulos) and all(r == "Info" for r in rotulos)


def _alertas_do_envio(alertas):
    """Separa (areas do FPM com severidade info, alertas fora do esperado). O eco
    ARIA global e' tolerado; qualquer outro alerta e' 'fora do esperado'."""
    areas, fora = [], []
    for a in alertas or []:
        if ("sapLsFPMMessageArea" in (a.get("classe") or "").split()
                and a.get("role") == "presentation"
                and a.get("severidade") == "info"):
            areas.append(a)
        elif not (a.get("id") == "sapur-aria-alert" and a.get("role") == "alert"
                  and "#sapur-aria-dynamic-texts" in (a.get("caminho") or "")
                  and (re.fullmatch(r"\d+ messages?", a.get("texto") or "")
                       or a.get("duplicado_de"))):
            fora.append(a)
    return areas, fora


def _sinais_submit_confirmado(cr_number, est, leituras, workflow_log):
    """O que FALTA para o Submit contar como ENVIADO (lista vazia = enviado).

    Os quatro sinais, iguais no envio manual da 90000002 e no automatico da
    90000003 (29/09, gotcha 20), exigidos JUNTOS:
      1. a 1a mensagem da area do FPM e' "You have submitted change request
         <esta CR>" e a area so' tem rotulos "Info.";
      2. Submit e Withdraw Draft sumiram;
      3. um CONTROLE com o texto exato "Workflow Log" esta na tela;
      4. Save e Cancel presentes e cinza (habilitado is False, explicito).
    O STATUS da CR fica de fora de proposito: 8 s depois do clique automatico
    ele ainda dizia "Changes to Be Executed". "Check" tambem varia.

    FAIL-CLOSED nas leituras (NO-GO do Codex, 52a entrada): `leituras` traz o
    resultado COMPLETO de _estado_acao_raiz para "Withdraw Draft", "Save" e
    "Cancel", ou None se a leitura levantou. Leitura com erro, modal, alerta
    fora do esperado ou sem lista de candidatos NUNCA conta como "botao
    ausente" - senao uma leitura ilegivel de Withdraw ajudaria a confirmar o
    envio e liberaria a proxima CR do lote.
    """
    if not isinstance(est, dict) or est.get("modais") or est.get("erro"):
        return ["modal/dialogo aberto ou leitura do Submit ilegivel"]
    faltam = []

    areas, fora = _alertas_do_envio(est.get("alertas"))
    msg = f"You have submitted change request {cr_number}Info."
    if len(areas) != 1 or fora:
        faltam.append(f"area de mensagens inesperada (areas={len(areas)}, fora={len(fora)})")
    else:
        ta = " ".join((areas[0].get("texto") or "").split())
        if not ta.startswith(msg):
            faltam.append(f"a 1a mensagem nao e' {msg[:-5]!r}")
        if any(r != "Info" for r in re.findall(r"(Info|Warning|Error|Success)\.", ta)):
            faltam.append("ha mensagem que nao e' Info na area")
    if est.get("candidatos"):
        faltam.append("Submit ainda na tela")

    for nome in ("Withdraw Draft", "Save", "Cancel"):
        leit = (leituras or {}).get(nome)
        if not isinstance(leit, dict) or leit.get("erro"):
            faltam.append(f"leitura de {nome!r} falhou - nao conta como ausente")
            continue
        if leit.get("modais"):
            faltam.append(f"modal/dialogo na leitura de {nome!r}")
        if _alertas_do_envio(leit.get("alertas"))[1]:
            faltam.append(f"alerta inesperado na leitura de {nome!r}")
        cands = leit.get("candidatos")
        if not isinstance(cands, list):
            faltam.append(f"leitura de {nome!r} sem lista de candidatos")
        elif nome == "Withdraw Draft":
            if cands:
                faltam.append("Withdraw Draft ainda na tela")
        elif len(cands) != 1 or cands[0].get("habilitado") is not False:
            faltam.append(f"{nome} nao esta presente e cinza")

    if not (isinstance(workflow_log, int) and not isinstance(workflow_log, bool)
            and workflow_log >= 1):
        faltam.append("sem controle 'Workflow Log' na tela")
    return faltam


# Quantos elementos VISIVEIS, do tamanho de um controle, tem o texto EXATO do
# argumento. Prova que o controle ("Workflow Log") esta na tela, e nao so' que
# a palavra aparece em algum lugar do body (revisao do Codex, 52a entrada).
_JS_CONTAR_CONTROLE_TEXTO = r"""
const norm = (s) => (s || '').replace(/\u00ad/g, '').replace(/\s+/g, ' ').trim();
const alvo = norm(arguments[0]);
let n = 0;
for (const el of document.querySelectorAll('a, button, span, div, [role="button"], [role="link"]')) {
    if (norm(el.innerText) !== alvo) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.width <= 300 && r.height <= 60) n++;
}
return n;
"""


def _poscondicao_submit(driver, cr_number):
    """Registra o efeito de UM clique em Submit sem inventar sucesso.

    O criterio positivo observado para Save (o mesmo botao continua na tela e
    desabilita) NAO vale para Submit: ele pode navegar, trocar o status da CR ou
    retirar as acoes da raiz. Como o clique ja teve efeito potencialmente
    irreversivel, qualquer falha de leitura posterior tambem significa
    **NAO REPETIR** - nunca uma autorizacao para tentar de novo.

    A rodada de 28/09 mostrou um estado negativo proprio: o clique pode apenas
    rodar uma checagem e deixar status/work item e Submit inalterados. Esse caso
    vira ``submit_parou_na_checagem``; continua NAO sendo sucesso e nunca
    autoriza um segundo clique. Qualquer outro estado segue
    ``efeito_desconhecido``. A funcao captura body, URL, janelas, mensagens e
    botoes para auditoria.
    """
    try:
        est, txt = _evidencia_pos_clique(driver, cr_number, "Submit")
    except Exception as e:
        log.error(
            f"[{cr_number}] Submit ja foi clicado, mas a pos-condicao falhou: "
            f"{type(e).__name__}: {e}. EFEITO DESCONHECIDO - NAO REPETIR.")
        return {
            "veredito": "efeito_desconhecido",
            "acao": "Submit",
            "erro_poscondicao": f"{type(e).__name__}: {e}",
            "nao_repetir": True,
        }

    alertas = est.get("alertas") or []
    modais = est.get("modais") or []
    candidatos = est.get("candidatos") or []
    bloqueantes = [a for a in alertas
                   if a.get("severidade") in _severidades_bloqueantes()]
    _registrar_html_alertas(driver, cr_number, "Submit")
    try:
        url = driver.current_url
    except Exception as e:
        url = f"<indisponivel: {type(e).__name__}: {e}>"
    try:
        janelas = list(driver.window_handles)
    except Exception as e:
        janelas = [f"<indisponivel: {type(e).__name__}: {e}>"]

    cr_na_tela = str(cr_number) in txt
    leituras = {}
    for nome in ("Withdraw Draft", "Save", "Cancel"):
        try:
            leituras[nome] = _estado_acao_raiz(driver, nome)
        except Exception as e:
            log.warning(f"[{cr_number}] pos-Submit: nao li o botao {nome!r}: {e}")
            leituras[nome] = None
    try:
        workflow_log = _execute_script_critico(driver, _JS_CONTAR_CONTROLE_TEXTO, "Workflow Log")
    except Exception as e:
        log.warning(f"[{cr_number}] pos-Submit: nao contei o controle 'Workflow Log': {e}")
        workflow_log = None
    faltam = _sinais_submit_confirmado(cr_number, est, leituras, workflow_log)
    if not faltam:
        veredito = "confirmado"
    elif _eh_submit_parou_na_checagem(cr_number, est, txt):
        veredito = "submit_parou_na_checagem"
    else:
        veredito = "efeito_desconhecido"

    if veredito == "confirmado":
        log.info(f"[{cr_number}] Submit CONFIRMADO: 'You have submitted change request', "
                 f"Submit e Withdraw Draft fora da tela, Workflow Log, Save/Cancel cinza.")
    elif veredito == "submit_parou_na_checagem":
        log.warning(
            f"[{cr_number}] Submit PAROU NA CHECAGEM (gotcha 20): status/work item inalterados "
            "e Submit ainda habilitado. NAO e' sucesso; NAO REPETIR.")
    else:
        log.warning(f"[{cr_number}] Submit sem os sinais de envio: faltam {faltam}. "
                    f"EFEITO DESCONHECIDO - NAO REPETIR.")
    log.info(
        f"[{cr_number}] pos-Submit: veredito={veredito!r} cr_na_tela={cr_na_tela} "
        f"url={url!r} janelas={janelas!r} modais={modais!r} bloqueantes={bloqueantes!r} "
        f"candidatos={candidatos!r} leituras={leituras!r} workflow_log={workflow_log!r}")
    log.info(f"[{cr_number}] pos-Submit tela[:1000]={txt[:1000]!r}")
    return {
        "veredito": veredito,
        "acao": "Submit",
        "nao_repetir": True,
        "segundo_clique_autorizado": False,
        "sinais_faltando": faltam,
        "cr_na_tela": cr_na_tela,
        "url": url,
        "janelas": janelas,
        "modais": modais,
        "alertas": alertas,
        "bloqueantes": bloqueantes,
        "candidatos": candidatos,
        "leituras": leituras,
        "workflow_log": workflow_log,
        "tela": txt[:2000],
    }


def fechar_subtelas_para_raiz(driver, cr_number, on_checkpoint=None):
    """Fecha as duas subtelas conhecidas e exige a raiz da CR correta.

    Responsabilidade deliberadamente separada de Save/Submit (gotcha 15). A
    conferencia da sessao B precisa voltar a raiz sem clicar Save; a sessao A
    reutiliza exatamente o mesmo fechamento antes de salvar.
    """
    _click_done(driver)
    try:
        wait_for_wda_ready(driver, timeout=20)
    except TimeoutException:
        time.sleep(2)
    t1 = _safe_execute_script(driver, "return document.body.innerText || '';") or ""
    if not t1:
        raise RuntimeError("finalize: tela vazia apos o 1o Done.")
    if "ERP Vendor Company Code:" in t1:
        raise RuntimeError("finalize: 1o Done nao fechou 'ERP Vendor Company Code' (secoes ainda visiveis).")
    if "ERP Vendor:" not in t1:
        raise RuntimeError(f"finalize: apos 1o Done nao reconheci a tela 'ERP Vendor'. tela: {t1[:200]!r}")
    _prog(on_checkpoint, passo=1, fase="apos o 1o Done (tela 'ERP Vendor')")
    pause("1o Done - fechou 'ERP Vendor Company Code', na tela 'ERP Vendor'")

    _click_done(driver)
    try:
        wait_for_wda_ready(driver, timeout=20)
    except TimeoutException:
        time.sleep(2)
    t2 = _safe_execute_script(driver, "return document.body.innerText || '';") or ""
    if not t2:
        raise RuntimeError("finalize: tela vazia apos o 2o Done.")
    if "ERP Vendor Company Code:" in t2 or "ERP Vendor:" in t2:
        raise RuntimeError(
            "finalize: 2o Done nao voltou pra CR - ainda ha secoes de 'ERP Vendor' "
            "(pode precisar de mais de 2 Done - P2)."
        )
    # marcador POSITIVO da tela raiz da CR (visto na rodada de 2026-09-10 14:31):
    # "Change Request" + os botoes de rodape "Save"/"Submit"/"Withdraw Draft".
    tem_cr = str(cr_number) in t2
    tem_acao_raiz = any(b in t2 for b in ("Withdraw Draft", "Submit", "Save"))
    if not (tem_cr and tem_acao_raiz):
        raise RuntimeError(
            f"finalize: apos 2o Done nao reconheci a tela raiz da CR "
            f"(cr_marker={tem_cr}, acao_raiz={tem_acao_raiz}). tela: {t2[:300]!r}"
        )
    log.info(f"[{cr_number}] de volta na tela raiz da CR apos 2 Done.")
    _prog(on_checkpoint, passo=2, fase="apos o 2o Done (tela raiz da CR)")
    pause("2o Done - de volta na tela raiz da CR")
    return t2


def salvar_cr_da_raiz(driver, cr_number):
    """Salva uma CR ja posicionada na raiz; nunca submete."""
    antes_save = _relatar_acao_raiz(driver, cr_number, "Save")
    pause_critica(f"PRESTES A SALVAR a CR {cr_number} (reversivel via 'Withdraw Draft')")
    _clicar_acao_raiz(driver, cr_number, "Save")
    veredito = _poscondicao_save(driver, cr_number, estado_antes=antes_save)
    pause("Save concluido - conferir a tela e o print antes de seguir")
    return veredito


def _estado_raiz_pronta_para_submit(driver, cr_number):
    """Leitura critica: CR certa, Save desabilitado e Submit unico/habilitado."""
    txt = _execute_script_critico(driver, "return document.body.innerText || '';") or ""
    if str(cr_number) not in txt:
        raise RuntimeError(
            f"Submit bloqueado: a CR {cr_number} nao esta visivel na raiz. "
            f"tela={txt[:200]!r}")

    est_save = _estado_acao_raiz(driver, "Save")
    est_submit = _estado_acao_raiz(driver, "Submit")
    for nome, est in (("Save", est_save), ("Submit", est_submit)):
        if est.get("erro"):
            raise RuntimeError(f"Submit bloqueado: nao li o estado de {nome}: {est['erro']}")
        if est.get("modais"):
            raise RuntimeError(
                f"Submit bloqueado: modal/dialogo na raiz: {est['modais']!r}")
        bloq = [a for a in (est.get("alertas") or [])
                if a.get("severidade") in _severidades_bloqueantes()]
        if bloq:
            raise RuntimeError(
                f"Submit bloqueado: mensagem bloqueante na raiz: {bloq!r}")

    saves = est_save.get("candidatos") or []
    saves_hab = [c for c in saves if c.get("habilitado")]
    if len(saves) != 1 or saves_hab:
        raise RuntimeError(
            "Submit bloqueado: a conferencia precisa terminar com exatamente 1 "
            f"Save existente e DESABILITADO; candidatos={saves!r}")

    submits = est_submit.get("candidatos") or []
    submits_hab = [c for c in submits if c.get("habilitado")]
    if len(submits_hab) != 1:
        raise RuntimeError(
            "Submit bloqueado: esperava exatamente 1 Submit habilitado; "
            f"candidatos={submits!r}")
    return {
        "cr_na_tela": True,
        "save_desabilitado": True,
        "submit_habilitado": True,
        "save": saves[0],
        "submit": submits_hab[0],
    }


# Quais Details o atestado do fluxo unico abre ANTES do Save (29/09).
# "amostra": o 1o e o ultimo codigo. O resto ja esta coberto: a insercao confere
# Type, Code e checkbox de cada codigo na sub-tela antes do Done, e a leitura
# integral no fim confere os 64 Types na tabela. Evidencia: 192/192 Details
# certos nas 3 CRs conferidas inteiras depois de inseridas (24/09, 28/09 e
# 29/09). "todos": os 64, como na 1a rodada real do fluxo unico (29/09) - custa
# ~9 min por CR (25 min em vez de ~16).
DETAILS_NO_FLUXO_UNICO = "amostra"


def _codes_details_do_atestado(esperados):
    """Codigos cujo Details o atestado precisa ter conferido."""
    if DETAILS_NO_FLUXO_UNICO == "todos":
        return list(esperados)
    if DETAILS_NO_FLUXO_UNICO == "amostra":
        return [esperados[0], esperados[-1]] if len(esperados) > 1 else list(esperados)
    raise ValueError(f"DETAILS_NO_FLUXO_UNICO invalido: {DETAILS_NO_FLUXO_UNICO!r}")


def _codes_do_atestado_insercao(codes=None):
    esperados = [str(c).strip().upper() for c in
                 (codes if codes is not None else TAX_CODES) if str(c).strip()]
    catalogo = [str(c).strip().upper() for c in TAX_CODES]
    if (not esperados or len(set(esperados)) != len(esperados)
            or Counter(esperados) != Counter(catalogo)):
        raise ValueError(
            "fluxo unico: exige exatamente TAX_CODES, sem vazio, falta ou duplicata.")
    return esperados


def iniciar_atestado_insercao(driver, cr_number, codes=None):
    """Preflight do fluxo unico: prova tabela vazia antes de qualquer New."""
    esperados = _codes_do_atestado_insercao(codes)
    sessao = getattr(driver, "session_id", None)
    if not sessao:
        raise RuntimeError("fluxo unico: navegador sem session_id; atestado bloqueado.")
    linhas, meta = ler_linhas_impostos(
        driver, strict_rowcount=True, com_meta=True, critico=True)
    tipos = [(ln.get("type") or "").strip().upper() for ln in linhas]
    if meta.get("n_tabelas_candidatas") != 1:
        raise RuntimeError(
            f"fluxo unico ({cr_number}): esperava exatamente 1 tabela no preflight, "
            f"achei {meta.get('n_tabelas_candidatas')!r}.")
    if tipos:
        raise RuntimeError(
            f"fluxo unico ({cr_number}): a CR precisa comecar LIMPA; encontrei {tipos}. "
            "Nao retomar nem salvar em cima de linhas preexistentes.")
    atestado = {
        "origem": "insercao",
        "fase": "aberta_limpa",
        "cr_number": str(cr_number),
        "session_id": sessao,
        "codes_esperados": esperados,
        "tabela_inicial_vazia": True,
        "aria_rowcount_inicial": meta.get("aria_rowcount"),
        "tabelas_candidatas_inicial": meta.get("n_tabelas_candidatas"),
        "inseridos": [],
        "pulados": [],
        "faltavam": [],
        "registro_inseridos": [],
        "detalhes_validados": [],
        "presentes": [],
        "faltando": list(esperados),
        "inesperados": [],
        "duplicados": {},
        "descricoes_vazias": [],
        "linhas_lidas": 0,
        "aria_rowcount": None,
        "tabelas_candidatas": None,
        "apto_para_save": False,
        "consumido_para_save": False,
        "save_veredito": None,
        "save_desabilitado": False,
        "submit_habilitado": False,
        "apto_para_submit": False,
    }
    log.info(f"[{cr_number}] fluxo unico: preflight provou tabela vazia: {atestado!r}")
    return atestado


def atestar_insercao_para_save(driver, cr_number, atestado, resumo,
                                on_progresso=None):
    """Completa o atestado proprio: 64 Details + leitura integral final + raiz."""
    if not isinstance(atestado, dict) or atestado.get("origem") != "insercao":
        raise RuntimeError("fluxo unico: atestado inicial de insercao ausente.")
    if atestado.get("fase") != "aberta_limpa":
        raise RuntimeError(
            f"fluxo unico: fase inicial invalida {atestado.get('fase')!r}.")
    if (str(atestado.get("cr_number")) != str(cr_number)
            or atestado.get("session_id") != getattr(driver, "session_id", None)):
        raise RuntimeError("fluxo unico: atestado inicial pertence a outra CR/sessao.")
    esperados = _codes_do_atestado_insercao(atestado.get("codes_esperados"))
    if not isinstance(resumo, dict):
        raise RuntimeError("fluxo unico: resumo da insercao ausente.")
    inseridos = [str(c).strip().upper() for c in (resumo.get("inseridos") or [])]
    pulados = [str(c).strip().upper() for c in (resumo.get("pulados") or [])]
    faltavam = [str(c).strip().upper() for c in (resumo.get("faltavam") or [])]
    registro = inseridos_nesta_execucao(cr_number)
    if (inseridos != esperados or pulados or faltavam != esperados
            or Counter(registro) != Counter(esperados)):
        raise RuntimeError(
            f"fluxo unico ({cr_number}): insercao nao foi integral e limpa. "
            f"inseridos={inseridos} pulados={pulados} faltavam={faltavam} "
            f"registro={registro}.")
    atestado.update({
        "fase": "conferindo_insercao",
        "inseridos": inseridos,
        "pulados": pulados,
        "faltavam": faltavam,
        "registro_inseridos": registro,
    })

    a_conferir = _codes_details_do_atestado(esperados)
    for i, code in enumerate(a_conferir, start=1):
        _prog(on_progresso, i=i, n=len(a_conferir), code=code,
              fase="conferindo Details da propria insercao")
        try:
            validar_linha_imposto(driver, code)
        except Exception:
            log.error(
                f"[{cr_number}] atestado da insercao falhou no Details de {code}; "
                f"validados={atestado['detalhes_validados']!r}")
            raise
        atestado["detalhes_validados"].append(code)

    linhas, meta = ler_linhas_impostos(
        driver, strict_rowcount=True, com_meta=True, critico=True)
    tipos = [(ln.get("type") or "").strip().upper() for ln in linhas]
    cont_esperados, cont_lidos = Counter(esperados), Counter(tipos)
    atestado.update({
        "presentes": [c for c in esperados if cont_lidos[c] > 0],
        "faltando": list((cont_esperados - cont_lidos).elements()),
        "inesperados": list((cont_lidos - cont_esperados).elements()),
        "duplicados": {c: n for c, n in cont_lidos.items()
                       if n > cont_esperados.get(c, 0)},
        "descricoes_vazias": [ln.get("type") for ln in linhas
                               if not (ln.get("description") or "").strip()],
        "linhas_lidas": len(linhas),
        "aria_rowcount": meta.get("aria_rowcount"),
        "tabelas_candidatas": meta.get("n_tabelas_candidatas"),
    })
    if (atestado["tabelas_candidatas"] != 1
            or cont_lidos != cont_esperados
            or atestado["descricoes_vazias"]):
        raise RuntimeError(
            f"fluxo unico ({cr_number}): leitura integral final recusada: {atestado!r}")

    atestado["fase"] = "voltando_raiz"
    fechar_subtelas_para_raiz(driver, cr_number)
    atestado["fase"] = "insercao_atestada"
    atestado["apto_para_save"] = True
    _validar_atestado_insercao(
        driver, cr_number, atestado, fase_esperada="insercao_atestada")
    log.info(f"[{cr_number}] ATESTADO da propria insercao: {atestado!r}")
    return atestado


def _validar_atestado_insercao(driver, cr_number, atestado, fase_esperada=None):
    """Recalcula as invariantes do atestado; nao confia apenas no booleano apto."""
    if not isinstance(atestado, dict) or atestado.get("origem") != "insercao":
        raise RuntimeError("bloqueado: atestado proprio da insercao ausente.")
    if str(atestado.get("cr_number")) != str(cr_number):
        raise RuntimeError("bloqueado: atestado de insercao pertence a outra CR.")
    sessao = getattr(driver, "session_id", None)
    if not sessao or atestado.get("session_id") != sessao:
        raise RuntimeError("bloqueado: atestado de insercao pertence a outra sessao.")
    if fase_esperada is not None and atestado.get("fase") != fase_esperada:
        raise RuntimeError(
            f"bloqueado: fase do atestado={atestado.get('fase')!r}; "
            f"esperava {fase_esperada!r}.")
    esperados = _codes_do_atestado_insercao(atestado.get("codes_esperados"))
    registro_vivo = inseridos_nesta_execucao(cr_number)
    if (atestado.get("tabela_inicial_vazia") is not True
            or atestado.get("aria_rowcount_inicial") != 1
            or atestado.get("tabelas_candidatas_inicial") != 1
            or atestado.get("inseridos") != esperados
            or atestado.get("pulados")
            or atestado.get("faltavam") != esperados
            or Counter(atestado.get("registro_inseridos") or []) != Counter(esperados)
            or Counter(registro_vivo) != Counter(esperados)
            or Counter(atestado.get("detalhes_validados") or [])
            != Counter(_codes_details_do_atestado(esperados))
            or atestado.get("presentes") != esperados
            or atestado.get("faltando")
            or atestado.get("inesperados")
            or atestado.get("duplicados")
            or atestado.get("descricoes_vazias")
            or atestado.get("linhas_lidas") != len(esperados)
            or atestado.get("aria_rowcount") != len(esperados) + 1
            or atestado.get("tabelas_candidatas") != 1
            or atestado.get("apto_para_save") is not True):
        raise RuntimeError(
            f"bloqueado: atestado da insercao inconsistente/incompleto: {atestado!r}")
    return True


def salvar_insercao_para_submit(driver, cr_number, atestado):
    """Consome uma vez o atestado proprio, salva e exige a raiz pronta."""
    _validar_atestado_insercao(
        driver, cr_number, atestado, fase_esperada="insercao_atestada")
    if atestado.get("consumido_para_save"):
        raise RuntimeError("Save bloqueado: este atestado de insercao ja foi consumido.")
    atestado["consumido_para_save"] = True
    atestado["fase"] = "save_em_andamento"
    try:
        veredito = salvar_cr_da_raiz(driver, cr_number) or {}
    except AntesDoClique:
        atestado["fase"] = "save_recusado_sem_clique"
        raise
    except Exception:
        atestado["fase"] = "persistencia_incerta"
        raise
    atestado["save_veredito"] = veredito.get("veredito")
    atestado["save"] = veredito
    if atestado["save_veredito"] != "confirmado":
        atestado["fase"] = "persistencia_incerta"
        log.warning(
            f"[{cr_number}] fluxo unico: Save={atestado['save_veredito']!r}; "
            "Submit NAO autorizado e a CR NAO deve ser repetida.")
        return atestado
    atestado["fase"] = "save_confirmado"
    try:
        raiz = _estado_raiz_pronta_para_submit(driver, cr_number)
    except Exception:
        atestado["fase"] = "salva_nao_submetida"
        raise
    atestado["save_desabilitado"] = raiz["save_desabilitado"]
    atestado["submit_habilitado"] = raiz["submit_habilitado"]
    atestado["apto_para_submit"] = True
    atestado["fase"] = "raiz_pronta_submit"
    log.info(f"[{cr_number}] insercao atestada + Save CONFIRMADO: apta a UM Submit.")
    return atestado


def conferir_impostos_no_servidor(driver, cr_number, codes=None,
                                   detalhes_codes=None, on_progresso=None):
    """Confere o Save em sessao nova e devolve o atestado da conferencia.

    O contrato e' **sem persistencia**, nao literalmente read-only: navegar e
    abrir/fechar Details trava a CR e cada Done reenvia os mesmos valores ao
    buffer. Esta funcao nunca clica New/Delete/Edit/Save/Submit/Withdraw.

    Termina na raiz com o Save HABILITADO - o SAP marca alteracao pendente
    depois dos Details (gotcha 19). Por isso ela NAO autoriza Submit: produz
    ``apto_para_save``, consumido por ``salvar_conferida_para_submit``, que
    salva e so entao pode marcar ``apto_para_submit``.

    ``detalhes_codes=None`` valida todos os Details e pode produzir
    ``apto_para_save=True``. Uma amostra explicita serve apenas para
    diagnostico e sempre volta inapta.
    """
    esperados = [str(c).strip().upper() for c in (codes if codes is not None else TAX_CODES)
                 if str(c).strip()]
    if not esperados or len(set(esperados)) != len(esperados):
        raise ValueError("conferencia: lista esperada vazia ou com codigos duplicados.")

    selecionados = (list(esperados) if detalhes_codes is None else
                    [str(c).strip().upper() for c in detalhes_codes if str(c).strip()])
    fora = [c for c in selecionados if c not in esperados]
    if fora or len(set(selecionados)) != len(selecionados):
        raise ValueError(
            f"conferencia: amostra de Details invalida (fora={fora}, valores={selecionados}).")

    relatorio = {
        "cr_number": str(cr_number),
        "session_id": getattr(driver, "session_id", None),
        "codes_esperados": esperados,
        "presentes": [],
        "faltando": [],
        "inesperados": [],
        "duplicados": {},
        "descricoes_vazias": [],
        "linhas_lidas": 0,
        "aria_rowcount": None,
        "tabelas_candidatas": None,
        "detalhes_validados": [],
        "apto_para_save": False,
        "save_veredito": None,
        "save_desabilitado": False,
        "submit_habilitado": False,
        "apto_para_submit": False,
    }

    navigate_to_tax_section(driver)
    linhas, meta = ler_linhas_impostos(driver, strict_rowcount=True, com_meta=True)
    tipos = [(ln.get("type") or "").strip().upper() for ln in linhas]
    cont_esperados, cont_lidos = Counter(esperados), Counter(tipos)
    relatorio.update({
        "presentes": [c for c in esperados if cont_lidos[c] > 0],
        "faltando": list((cont_esperados - cont_lidos).elements()),
        "inesperados": list((cont_lidos - cont_esperados).elements()),
        "duplicados": {c: n for c, n in cont_lidos.items() if n > cont_esperados.get(c, 0)},
        "descricoes_vazias": [ln.get("type") for ln in linhas
                               if not (ln.get("description") or "").strip()],
        "linhas_lidas": len(linhas),
        "aria_rowcount": meta.get("aria_rowcount"),
        "tabelas_candidatas": meta.get("n_tabelas_candidatas"),
    })
    if relatorio["tabelas_candidatas"] != 1:
        raise RuntimeError(
            f"conferencia ({cr_number}): esperava exatamente 1 tabela de impostos, "
            f"achei {relatorio['tabelas_candidatas']!r}. Submit bloqueado por ambiguidade.")
    tabela_exata = cont_lidos == cont_esperados
    if not tabela_exata or relatorio["descricoes_vazias"]:
        log.error(f"[{cr_number}] conferencia do servidor FALHOU na tabela: {relatorio!r}")
        raise RuntimeError(
            f"conferencia ({cr_number}): tabela nao corresponde exatamente ao esperado. "
            f"faltando={relatorio['faltando']} inesperados={relatorio['inesperados']} "
            f"duplicados={relatorio['duplicados']} "
            f"descricoes_vazias={relatorio['descricoes_vazias']}")

    for i, code in enumerate(selecionados, start=1):
        _prog(on_progresso, i=i, n=len(selecionados), code=code,
              fase="conferindo Details salvo no servidor")
        try:
            validar_linha_imposto(driver, code)
        except Exception:
            log.error(
                f"[{cr_number}] conferencia falhou no Details de {code}; validados ate "
                f"aqui={relatorio['detalhes_validados']!r}")
            raise
        relatorio["detalhes_validados"].append(code)

    fechar_subtelas_para_raiz(driver, cr_number)
    validou_todos = Counter(relatorio["detalhes_validados"]) == cont_esperados
    relatorio["apto_para_save"] = bool(
        tabela_exata and not relatorio["descricoes_vazias"] and validou_todos
        and relatorio["session_id"])

    try:
        driver.save_screenshot(os.path.join(
            PRINTS_DIR,
            f"conferencia_submit_{cr_number}_{datetime.now():%Y%m%d_%H%M%S}.png"))
    except Exception:
        pass
    log.info(f"[{cr_number}] ATESTADO da conferencia no servidor: {relatorio!r}")
    if not validou_todos:
        log.warning(
            f"[{cr_number}] conferencia por AMOSTRA concluida, mas NAO autoriza Save nem "
            f"Submit: {len(relatorio['detalhes_validados'])}/{len(esperados)} Details validados.")
    return relatorio


def _validar_atestado_conferencia(driver, cr_number, atestado):
    """A conferencia desta CR, nesta sessao, com TODOS os Details - antes do Save."""
    if not isinstance(atestado, dict) or not atestado.get("apto_para_save"):
        raise RuntimeError("bloqueado: atestado da conferencia ausente ou inapto.")
    if str(atestado.get("cr_number")) != str(cr_number):
        raise RuntimeError(
            f"bloqueado: atestado e' da CR {atestado.get('cr_number')!r}, "
            f"nao da CR {cr_number}.")
    sessao_atual = getattr(driver, "session_id", None)
    if not sessao_atual or atestado.get("session_id") != sessao_atual:
        raise RuntimeError(
            "bloqueado: o atestado nao pertence a esta mesma sessao do navegador.")
    esperados = atestado.get("codes_esperados") or []
    detalhes = atestado.get("detalhes_validados") or []
    if (not esperados or Counter(detalhes) != Counter(esperados)
            or atestado.get("faltando") or atestado.get("inesperados")
            or atestado.get("duplicados") or atestado.get("descricoes_vazias")
            or atestado.get("tabelas_candidatas") != 1):
        raise RuntimeError(
            f"bloqueado: atestado da conferencia inconsistente/incompleto: {atestado!r}")
    return True


def salvar_conferida_para_submit(driver, cr_number, atestado):
    """Sessao B, passo 2: SALVA a CR conferida e so entao a declara apta ao Submit.

    O Save e' OBRIGATORIO aqui (gotcha 19, ensaio de 24/09): abrir os Details
    deixa alteracao pendente e o Save habilitado. Salvar consolida exatamente o
    estado que acabou de ser conferido e leva a raiz ao estado em que o Submit
    foi visto habilitado em 23/09 - Save cinza.

    Regras (Codex, 36a entrada):
      - so salva com atestado COMPLETO desta CR e desta sessao;
      - so marca ``apto_para_submit`` com Save ``confirmado`` E raiz pronta
        (Save unico e desabilitado, Submit unico e habilitado, sem modal nem
        mensagem bloqueante);
      - Save com ``ressalva`` NUNCA autoriza Submit. A regra do alerta duplicado
        pos-Save ainda precisa ser escrita com o HTML real
        (_registrar_html_alertas) - ate la, ressalva para aqui.
    ``problema``/``indeterminado`` levantam dentro de salvar_cr_da_raiz.
    """
    try:
        _validar_atestado_conferencia(driver, cr_number, atestado)
    except RuntimeError as e:
        raise AntesDoClique(str(e)) from None
    veredito = salvar_cr_da_raiz(driver, cr_number) or {}
    atestado["save_veredito"] = veredito.get("veredito")
    atestado["save"] = veredito
    if atestado["save_veredito"] != "confirmado":
        log.warning(
            f"[{cr_number}] Save da sessao B terminou como {atestado['save_veredito']!r}: "
            f"Submit NAO autorizado. O Save provavelmente gravou - NAO repita; confira a "
            f"CR e o HTML das mensagens no log.")
        return atestado
    raiz = _estado_raiz_pronta_para_submit(driver, cr_number)
    atestado["save_desabilitado"] = raiz["save_desabilitado"]
    atestado["submit_habilitado"] = raiz["submit_habilitado"]
    atestado["apto_para_submit"] = True
    log.info(f"[{cr_number}] conferida + Save CONFIRMADO: apta ao Submit.")
    return atestado


def _validar_atestado_submit(driver, cr_number, atestado):
    if not isinstance(atestado, dict) or not atestado.get("apto_para_submit"):
        raise RuntimeError("Submit bloqueado: atestado ausente ou inapto.")
    try:
        if atestado.get("origem") == "insercao":
            _validar_atestado_insercao(
                driver, cr_number, atestado, fase_esperada="raiz_pronta_submit")
            if atestado.get("consumido_para_save") is not True:
                raise RuntimeError(
                    "bloqueado: atestado da insercao nao foi consumido pelo Save.")
        else:
            _validar_atestado_conferencia(driver, cr_number, atestado)
    except RuntimeError as e:
        raise RuntimeError(f"Submit {e}") from None
    if (atestado.get("save_veredito") != "confirmado"
            or not atestado.get("save_desabilitado")
            or not atestado.get("submit_habilitado")):
        raise RuntimeError(
            "Submit bloqueado: exige o Save CONFIRMADO desta sessao, com Save desabilitado "
            f"e Submit habilitado depois dele: {atestado!r}")
    return True


def _clicar_submit_validado_da_raiz(driver, cr_number):
    r = _execute_script_critico(
        driver, _JS_CLICAR_SUBMIT_VALIDADO, str(cr_number),
        list(_severidades_bloqueantes()))
    if not isinstance(r, dict) or not r.get("ok"):
        raise RuntimeError(
            f"Submit: clique atomico recusado - {(r or {}).get('erro', r)!r}")
    log.info(f"[{cr_number}] Submit clicado SEM novo Save: {r.get('clicado')!r}")
    return r.get("clicado")


def reabrir_cr_para_submit(driver, cr_number):
    """Fecha a janela da CR e reabre pela lista, no MESMO navegador (gotcha 20).

    28/09: o Submit logo depois de Details + Save, na mesma janela, so' rodou a
    checagem. 29/09: numa CR recem-aberta, um clique (manual e automatico)
    enviou. Um driver novo exigiria login por CR - inviavel pra 28 CRs do mes -
    entao a CR e' reaberta numa janela nova. Nao abre Details e nao salva.
    Prova: a janela antiga fechou, a nova e' outra, a CR certa esta nela,
    destravada, com status/work item de antes do Submit e a raiz pronta.
    Levanta em qualquer desvio; quem chama registra salva_nao_submetida.
    """
    antiga = driver.current_window_handle
    if len(driver.window_handles) < 2:
        raise RuntimeError("reabrir: esperava a janela da CR e a do shell abertas.")
    driver.close()
    restantes = list(driver.window_handles)
    if antiga in restantes or not restantes:
        raise RuntimeError(f"reabrir: a janela da CR ({antiga}) nao fechou.")
    # a 1a janela e' a do shell original: nos modos que persistem o lote deixa
    # so' ela + a da CR no comeco de cada CR (_fechar_janelas_extras)
    driver.switch_to.window(restantes[0])
    driver.get(ISHIFT_URL)
    go_to_display_change_requests(driver)
    antes = set(driver.window_handles)
    open_cr(driver, cr_number)
    nova = driver.current_window_handle
    if nova == antiga or nova in antes or nova not in driver.window_handles:
        raise RuntimeError("reabrir: a CR nao abriu numa janela nova.")
    if cr_esta_travada(driver):
        raise RuntimeError(f"reabrir: a CR {cr_number} aparece travada.")
    texto = " ".join(str(_execute_script_critico(
        driver, "return document.body.innerText || '';") or "").split())
    if not _estado_aceita_submit(texto, cr_number):
        i = texto.find("Process Data")
        raise RuntimeError(
            f"reabrir: status/work item da CR {cr_number} nao sao de CR nova nem devolvida "
            f"pra revisao: {texto[i:i + 200] if i >= 0 else texto[:200]!r}")
    raiz = _estado_raiz_pronta_para_submit(driver, cr_number)
    log.info(f"[{cr_number}] CR reaberta pra Submit: janela {antiga} -> {nova}")
    return {"janela_antiga": antiga, "janela_nova": nova,
            "save_desabilitado": raiz["save_desabilitado"],
            "submit_habilitado": raiz["submit_habilitado"]}


def submeter_cr_validada_da_raiz(driver, cr_number, atestado):
    """Consome atestado da sessao B ou da insercao e clica Submit UMA vez,
    so' na janela da CR REABERTA depois do Save (gotcha 20)."""
    _validar_atestado_submit(driver, cr_number, atestado)
    reab = atestado.get("reaberta") if isinstance(atestado, dict) else None
    if (not isinstance(reab, dict) or not reab.get("janela_nova")
            or reab.get("janela_nova") == reab.get("janela_antiga")
            or driver.current_window_handle != reab.get("janela_nova")):
        raise RuntimeError(
            "Submit bloqueado: a CR precisa ser REABERTA depois do Save e o Submit sai "
            "nessa janela nova (gotcha 20).")
    _estado_raiz_pronta_para_submit(driver, cr_number)
    pause_critica(
        f"PRESTES A SUBMETER a CR {cr_number} - manda pra APROVACAO, NAO e' reversivel")
    if atestado.get("origem") == "insercao":
        atestado["fase"] = "submit_em_andamento"
    atestado["submit_clicado"] = True     # conservador: marcado ANTES do clique
    _clicar_submit_validado_da_raiz(driver, cr_number)
    # A partir daqui QUALQUER resultado e' "nao repetir": o clique irreversivel
    # ja aconteceu. A pos-condicao captura evidencia, mas ainda nao declara OK.
    veredito = _poscondicao_submit(driver, cr_number)
    if atestado.get("origem") == "insercao":
        atestado["fase"] = veredito.get("veredito", "submit_efeito_desconhecido")
    pause("Submit clicado - efeito ainda precisa ser conferido no SAP e no print")
    return veredito


def finalize_cr(driver, cr_number, salvar=False, submeter=False, on_checkpoint=None):
    """Compatibilidade do fluxo da sessao A: fecha e opcionalmente salva.

    Submit foi removido DESTE fluxo de proposito (gotcha 15). Ele so pode sair
    pela sessao B: ``conferir_impostos_no_servidor`` ->
    ``salvar_conferida_para_submit`` (Save confirmado, gotcha 19) ->
    ``submeter_cr_validada_da_raiz``.
    """
    if submeter:
        raise RuntimeError(
            "finalize_cr nao submete mais: use a sessao B (conferencia completa + Save "
            "confirmado) e submeter_cr_validada_da_raiz().")
    log.info(f"Finalizando CR {cr_number} (salvar={salvar}, submeter=False)...")
    fechar_subtelas_para_raiz(driver, cr_number, on_checkpoint=on_checkpoint)

    if not salvar:
        log.warning(f"[{cr_number}] finalize SEM Save/Submit - nada persistido (rodada de teste).")
        return {}
    return {"Save": salvar_cr_da_raiz(driver, cr_number)}


def get_cr_list():
    """
    Le os numeros de CR do terminal, VALIDA e DEDUPLICA (9a revisao do Codex).

    Antes aceitava qualquer coisa: "90000001, abc, 90000001" virava tres
    entradas, a duplicada reabria a mesma CR na mesma execucao e o "abc" so
    falhava la na frente - depois de abrir o navegador, fazer login e entrar no
    app, ou seja, no ponto mais caro possivel.
    """
    print("\nCole os numeros das CRs (uma por linha, ou separadas por virgula).")
    print("Pressione ENTER duas vezes para avancar:\n")
    brutos = []
    while True:
        try:
            line = input().strip()
        except EOFError:
            _marcar_stdin_sem_resposta()
            break
        if not line:
            break
        brutos.extend([c.strip().lstrip(BOM) for c in line.replace(";", ",").split(",")
                       if c.strip().lstrip(BOM)])

    crs, vistos, invalidos, repetidos = [], set(), [], []
    for c in brutos:
        if not c.isdigit():
            invalidos.append(c)
            continue
        if c in vistos:
            repetidos.append(c)
            continue
        vistos.add(c)
        crs.append(c)          # ordem preservada

    if invalidos:
        print(f"\n  IGNORADAS (nao sao numeros): {invalidos}")
    if repetidos:
        print(f"  IGNORADAS (repetidas): {repetidos}")
    return crs, bool(invalidos)


# --- Modos mutuamente exclusivos de persistencia (gotchas 15, 19 e 20) ---
# SALVAR=True: sessao A, insere os codigos e faz apenas Save.
# SUBMETER=True: sessao B NOVA, confere tabela + TODOS os 64 Details, volta a
# raiz, SALVA de novo (os Details deixam alteracao pendente - gotcha 19) e so
# com esse Save CONFIRMADO faz o Submit.
# INSERIR_SALVAR_SUBMETER=True: fluxo unico, com atestado proprio da insercao,
# 64 Details, leitura integral, Save confirmado e UM clique em Submit. O 2o
# clique segue proibido: gotcha 20 mostrou que o primeiro pode so' checar.
# Mais de um modo True e' configuracao invalida. Todos False = teste.
# Uso: o pacote da usuaria liga SO o fluxo unico (codigo/gerar_pacote.py).
# SALVAR=True e SUBMETER=True sao modos de DESENVOLVIMENTO, sem suporte em
# producao: o SALVAR ainda nao exige tabela vazia nem conjunto final exato e
# trata erro depois do clique do Save como falha comum (QA de 06/10, achados
# 1 e 3, adiados com o Codex na 62a entrada do quadro).
SALVAR = False
SUBMETER = False
INSERIR_SALVAR_SUBMETER = False

# Durante a fase de validacao, parar o lote inteiro no 1o erro (nao seguir pra
# proxima CR - evita deixar varias CRs meio-alteradas/travadas).
PARAR_LOTE_NO_ERRO = True

# Minutos por CR na tela de confirmacao - numero fixo, pedido do operador em
# 28/09. Inserir: 64 x ~12,7 s (14/09) + navegacao e Save. Conferir e
# submeter: 64 Details em ~9 min (ensaio de 24/09) + Save e Submit. Os mesmos
# valores do README; o login fica de fora (e' um so por execucao).
MINUTOS_POR_CR = {"inserir_e_salvar": 15, "inserir_sem_persistir": 15,
                  "conferir_e_submeter": 12,
                  "inserir_salvar_submeter": 16}


def _estimativa_tempo(n_crs, modo):
    """'45 min' / '7h00' pra n_crs CRs no modo dado."""
    h, m = divmod(n_crs * MINUTOS_POR_CR[modo], 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _modo_execucao(salvar=None, submeter=None, inserir_salvar_submeter=None):
    """Normaliza os tres modos de execucao, que sao mutuamente exclusivos."""
    salvar = SALVAR if salvar is None else bool(salvar)
    submeter = SUBMETER if submeter is None else bool(submeter)
    fluxo_unico = (INSERIR_SALVAR_SUBMETER if inserir_salvar_submeter is None
                   else bool(inserir_salvar_submeter))
    if sum((salvar, submeter, fluxo_unico)) > 1:
        raise ValueError(
            "SALVAR, SUBMETER e INSERIR_SALVAR_SUBMETER sao modos mutuamente exclusivos.")
    if fluxo_unico:
        return "inserir_salvar_submeter"
    if submeter:
        return "conferir_e_submeter"
    if salvar:
        return "inserir_e_salvar"
    return "inserir_sem_persistir"


def _descricao_modo(modo):
    if modo == "inserir_salvar_submeter":
        conferencia = ("conferir TODOS os 64 Details" if DETAILS_NO_FLUXO_UNICO == "todos"
                       else "conferir por amostra o 1o e o ultimo Details, reler as 64 linhas")
        return (f"inserir os 64 codigos, {conferencia}, SALVAR e ENVIAR para aprovacao "
                f"(um unico clique em Submit, nunca repetido)")
    if modo == "conferir_e_submeter":
        return ("conferir no servidor os 64 codigos e TODOS os Details, "
                "SALVAR e depois clicar SUBMIT uma vez")
    if modo == "inserir_e_salvar":
        return "inserir os 64 codigos e SALVAR (sem submeter)"
    return "inserir os 64 codigos SEM gravar nada (rascunho/teste)"


def _confirmacao_modo(modo, ambiente=None):
    if modo == "inserir_salvar_submeter":
        esperado = "SALVAR E SUBMETER"
    elif modo == "conferir_e_submeter":
        esperado = "SUBMETER"
    elif modo == "inserir_e_salvar":
        esperado = "SALVAR"
    else:
        esperado = "CONFIRMAR"
    if (AMBIENTE if ambiente is None else ambiente) == "producao":
        esperado = "PRODUCAO " + esperado
    return esperado


def main():
    _ativar_log_em_arquivo()
    # No arquivo (diagnostico), nao na tela da usuaria: qual ambiente e qual URL.
    log.debug(f"Ambiente configurado (config.py): {AMBIENTE} -> {ISHIFT_URL}")
    try:
        modo = _modo_execucao()
    except ValueError as e:
        log.error(f"CONFIG invalida: {e} Encerrando.")
        sys.exit(2)          # config invalida nao pode terminar com exit 0


    modo_persiste = modo != "inserir_sem_persistir"
    if modo_persiste and CHECKPOINT_ANTES_DE_PERSISTIR and _stdin_parece_console() is False:
        log.warning(
            "stdin nao parece um console do Windows. Se for isso mesmo, o checkpoint "
            "antes do Save/Submit vai ABORTAR a rodada - e ai a CR ja estara travada. "
            "Se voce esta num terminal de verdade, ignore este aviso.")

    # O que a usuaria precisa saber antes de comecar, em portugues de gente: o
    # que vai acontecer com as CRs dela. O resto (ambiente, URL, aviso de
    # lock) e' nosso e esta no arquivo de log - o aviso de lock volta a
    # aparecer na hora em que ele importa, que e' quando uma CR falha.
    o_que_faz = _descricao_modo(modo)
    log.debug(f"modo={modo} SALVAR={SALVAR} SUBMETER={SUBMETER} "
              f"INSERIR_SALVAR_SUBMETER={INSERIR_SALVAR_SUBMETER}")
    print("")
    print("=" * 64)
    print("  Automacao de Withholding Tax")
    print(f"  Em cada CR: {o_que_faz}.")
    if modo in ("conferir_e_submeter", "inserir_salvar_submeter"):
        print("  ATENCAO: o Submit pode enviar a CR para APROVACAO e a automacao")
        print("  nao consegue desfazer isso. Se o 1o clique parar na checagem,")
        print("  a automacao PARA: o 2o clique continua proibido.")
    if AMBIENTE == "producao":
        print("")
        print("  *** AMBIENTE DE PRODUCAO - as alteracoes sao reais. ***")
    print("=" * 64)
    # Confirmacao escalonada: cada nivel de risco tem uma palavra diferente, pra
    # nao virar reflexo de digitar sempre a mesma coisa (7a revisao do Codex -
    # antes Save-only e Save+Submit usavam ambas "PERSISTIR").
    esperado = _confirmacao_modo(modo)
    # A LISTA VEM ANTES DA CONFIRMACAO (9a revisao do Codex). Antes o operador
    # digitava "SALVAR SUBMETER" sem nunca ter visto QUAIS CRs seriam afetadas -
    # e um numero digitado errado, mas que existe, seria processado assim mesmo.
    cr_list, teve_invalido = get_cr_list()
    if not cr_list:
        log.warning("Nenhuma CR valida informada. Encerrando.")
        sys.exit(2)
    # Aqui ja houve uma leitura de stdin (get_cr_list), entao da' pra AFIRMAR
    # se ha alguem do outro lado. Falhar agora, nao depois de 14 min de
    # insercao: sem quem responda, o checkpoint de Save/Submit abortaria com a
    # CR ja travada por horas - o pior dos dois mundos (10a revisao do Codex).
    if modo == "inserir_salvar_submeter" and teve_invalido:
        log.error(
            "fluxo unico recusado: a lista continha entrada invalida. Encerrando antes "
            "de abrir o navegador; confira e cole a lista inteira novamente.")
        sys.exit(2)
    if modo_persiste and CHECKPOINT_ANTES_DE_PERSISTIR and not tem_terminal_interativo():
        log.error(
            "SALVAR/SUBMETER com CHECKPOINT_ANTES_DE_PERSISTIR=True exigem um terminal "
            "que responda, e a leitura da lista de CRs ja bateu em EOF - ninguem vai "
            "poder confirmar o clique diante da tela. Rode num cmd/PowerShell de "
            "verdade ou pelo 'Rodar automacao.bat'. Encerrando ANTES de abrir o "
            "navegador, pra nao travar CR nenhuma a toa.")
        sys.exit(2)

    # A confirmacao logo abaixo tambem precisa de resposta. Sem terminal ela
    # levantaria EOFError cru; melhor uma mensagem que diga o que fazer.
    if not tem_terminal_interativo():
        log.error(
            "nao ha terminal para ler a confirmacao (stdin em EOF). Encerrando sem "
            "abrir o navegador. Rode num cmd/PowerShell ou pelo 'Rodar automacao.bat'.")
        sys.exit(2)

    if teve_invalido:
        print("\n  ATENCAO: havia entradas invalidas na lista (ignoradas acima).")

    print(f"\n{'-'*64}")
    print(f"  Vou processar {len(cr_list)} CR(s), nesta ordem:")
    for i, cr in enumerate(cr_list, start=1):
        print(f"    {i:>2}. {cr}")
    print(f"  E em cada uma: {o_que_faz}.")
    print(f"  Tempo estimado: cerca de {_estimativa_tempo(len(cr_list), modo)} "
          f"({MINUTOS_POR_CR[modo]} min por CR, medido em pre-producao), mais o tempo "
          f"do login. A previsao e' recalculada durante a execucao.")
    if not CHECKPOINT_ANTES_DE_PERSISTIR:
        # Esta confirmacao e' a UNICA autorizacao da execucao inteira (24/09).
        print("  Depois desta confirmacao o programa roda SOZINHO ate o fim, sem")
        print("  pedir mais nada - voce so precisa fazer o login no SAP.")
    print(f"{'-'*64}")

    try:
        resp = _pedir_confirmacao(esperado)
    except EOFError:
        _marcar_stdin_sem_resposta()
        log.error(
            "nao ha terminal para ler a confirmacao (stdin em EOF). Encerrando sem abrir "
            "o navegador. Rode num cmd/PowerShell ou pelo 'Rodar automacao.bat'.")
        sys.exit(2)
    if not resp:
        log.warning(f"Confirmacao nao recebida (esperava {esperado!r}). Encerrando sem rodar nada.")
        sys.exit(2)
    log.info(f"{len(cr_list)} CR(s) para processar: {cr_list}")

    driver = None
    try:
        driver = criar_driver_edge()
        driver.maximize_window()
        driver.get(ISHIFT_URL)
        _rodar_lote(driver, cr_list)
    finally:
        if driver is not None:
            try:
                input("\nPressione ENTER para fechar o navegador...")
            except EOFError:
                esperar_inspecao()      # sem TTY: nao fecha na cara do operador
            try:
                driver.quit()
            except Exception:
                log.warning("nao consegui fechar o navegador", exc_info=True)


def _fechar_janelas_extras(driver):
    """Deixa UMA janela - a primeira, do shell original - antes da proxima CR.

    Pendencia do Codex (52a entrada): sem isto cada CR deixava uma janela a
    mais (a da CR vira shell na iteracao seguinte, e o reabrir abre outra),
    28 CRs = 28+ janelas do Edge. So' e' chamado nos modos que PERSISTEM: la'
    a CR anterior terminou salva ou enviada, sem alteracao pendente - e o
    reabrir de 29/09 ja fechou a janela de uma CR salva sem dialogo nenhum.
    No modo sem persistir a CR anterior tem alteracao pendente e fechar pode
    disparar beforeunload, entao nao se mexe.
    Qualquer falha (janela que nao fecha, dialogo) levanta: quem chama para o
    lote ANTES de tocar na CR seguinte.
    """
    handles = list(driver.window_handles)
    if not handles:
        raise RuntimeError("nenhuma janela do navegador aberta.")
    manter = handles[0]
    for h in handles[1:]:
        driver.switch_to.window(h)
        driver.close()
    restantes = list(driver.window_handles)
    if restantes != [manter]:
        raise RuntimeError(f"janelas extras nao fecharam: {restantes}")
    driver.switch_to.window(manter)
    if len(handles) > 1:
        _execute_script_critico(driver, "return true;")   # portao: nenhum dialogo aberto
    return len(handles) - 1


def _submeter_no_lote(driver, cr_number, atestado, results):
    """Save CONFIRMADO -> reabre a CR -> UM Submit. True so' se o lote pode seguir.

    Falha antes do clique = CR SALVA e nao submetida (``salva_nao_submetida``);
    falha depois = ``submit_efeito_desconhecido``. Nos dois casos o lote para e a
    CR nao deve ser repetida as cegas.
    """
    if atestado.get("save_veredito") != "confirmado":
        raise RuntimeError(f"[{cr_number}] reabrir/Submit so' depois de Save CONFIRMADO.")
    try:
        aceitos_antes = _ALERTAS_ACEITOS
        atestado["reaberta"] = reabrir_cr_para_submit(driver, cr_number)
        if _ALERTAS_ACEITOS != aceitos_antes:
            raise RuntimeError(
                f"{_ALERTAS_ACEITOS - aceitos_antes} dialogo(s) do navegador aceito(s) em "
                f"silencio durante a reabertura - Submit recusado sem clicar.")
        veredito_submit = submeter_cr_validada_da_raiz(driver, cr_number, atestado)
    except Exception:
        estado = ("submit_efeito_desconhecido" if atestado.get("submit_clicado")
                  else "salva_nao_submetida")
        if atestado.get("origem") == "insercao":
            atestado["fase"] = estado
        log.exception(f"[{cr_number}] {estado}: Save CONFIRMADO. NAO repita esta CR as "
                      f"cegas - confira no SAP (salva_nao_submetida -> sessao B depois).")
        results["ressalva"].append((cr_number, estado, {"atestado": atestado}))
        return False
    return _registrar_resultado_submit(results, cr_number, atestado, veredito_submit)


def _registrar_resultado_submit(results, cr_number, atestado, veredito_submit):
    """UM clique em Submit ja aconteceu: registra com o veredito REAL e diz se o
    lote pode seguir. So' 'confirmado' segue; qualquer outro para o lote,
    independente de PARAR_LOTE_NO_ERRO - enquanto nao houver sinal positivo
    firmado, nao ha base para clicar o Submit da proxima CR."""
    pacote = {"atestado": atestado, "Submit": veredito_submit}
    v = str(veredito_submit.get("veredito"))
    if v == "confirmado":
        results["ok"].append((cr_number, "submetida", pacote))
        return True
    estado = v if v.startswith("submit_") else f"submit_clicado_{v}"
    log.warning(f"[{cr_number}] {estado} - NAO REPETIR: {pacote!r}")
    results["ressalva"].append((cr_number, estado, pacote))
    log.warning("PARANDO o lote depois do Submit: o clique irreversivel ja aconteceu "
                "e seu efeito ainda precisa ser conferido. NAO repita.")
    return False


_O_QUE_FOI_FEITO = {"submetida": "enviada para aprovacao",
                    "salva": "salva (nao enviada)",
                    "validada_sem_persistir": "conferida sem gravar (teste)"}


def _mensagem_final(results, nao_processadas):
    """Quadro final da tela, depois do resumo tecnico do log (pedido do operador,
    07/10): numa frase, se acabou bem e o que fazer. Mesma prioridade do exit
    code: ressalva > falha > lista incompleta > sem erros."""
    extra = ""
    if results["ressalva"]:
        titulo = ("FINALIZADO COM RESSALVA - NAO rode de novo: "
                  + ", ".join(cr for cr, _, _ in results["ressalva"]))
        dica = "Abra essas CRs no SAP e confira como ficaram antes de qualquer coisa."
    elif results["falha"]:
        titulo = "FINALIZADO COM ERRO em: " + ", ".join(results["falha"])
        dica = ("Nada foi gravado nelas, mas podem ficar travadas no SAP por algumas "
                "horas. Tente de novo mais tarde.")
        if results.get("sap_indisponivel"):
            # o motivo, nao um diagnostico: interromper nao prova queda do servidor
            motivos = sorted({cat for _, cat in results["sap_indisponivel"]})
            dica = (f"Parou por sinal de indisponibilidade ({', '.join(motivos)}; "
                    f"detalhes no log). " + dica)
        # gotcha 26 / Codex 76a: a conferencia e' o preflight da proxima rodada
        extra = ("Ao repetir, a automacao confere sozinha que a tabela esta vazia. Nao "
                 "abra os Details da CR so para olhar: isso a trava de novo.")
    elif nao_processadas:
        titulo, dica = "FINALIZADO SEM CONCLUIR A LISTA", ""
    else:
        titulo, dica = "FINALIZADO SEM ERROS", ""
    linhas = ["=" * 64, "  " + titulo]
    if dica:
        linhas.append("  " + dica)
    if extra:
        linhas.append("  " + extra)
    for cr, estado, _ in results["ok"]:
        linhas.append(f"  {cr}: {_O_QUE_FOI_FEITO.get(estado, estado)}")
    if nao_processadas:
        linhas.append("  Nao processadas (podem ir numa proxima execucao): "
                      + ", ".join(nao_processadas))
    linhas.append("=" * 64)
    return linhas


def _rodar_lote(driver, cr_list):
    """Processa a lista de CRs. Separado de main() pra o driver ficar sob um
    try/finally unico - antes, uma falha no login ou um EOFError no input final
    deixavam o Edge aberto e o driver.quit() sem rodar (9a revisao do Codex)."""
    esperar_login_manual(driver)

    results = {"ok": [], "falha": [], "ressalva": []}
    modo = _modo_execucao()

    # previsao do resto do lote pelas CRs concluidas NESTA execucao: a estimativa
    # fixa do inicio (MINUTOS_POR_CR) nao acompanha o SAP do dia (gotcha 26)
    duracoes, t_cr, n_ok = [], None, 0
    for idx, cr_number in enumerate(cr_list):
        if t_cr is not None and len(results["ok"]) > n_ok:
            duracoes.append(time.time() - t_cr)
        if duracoes:
            media = sum(duracoes) / len(duracoes)
            restantes = len(cr_list) - idx
            log.info(f"Previsao: {restantes} CR(s) restante(s), cerca de "
                     f"{restantes * media / 60:.0f} min (media de {media / 60:.0f} min por "
                     f"CR nesta execucao).")
        t_cr, n_ok = time.time(), len(results["ok"])
        if modo != "inserir_sem_persistir":
            try:
                _fechar_janelas_extras(driver)
            except Exception:
                # nao entra em "falha": a CR nem foi aberta. O resumo final a lista
                # como NAO processada, que e' o que ela e'.
                log.exception(
                    f"[{cr_number}] nao consegui fechar as janelas da CR anterior. NADA foi "
                    f"feito nesta CR; PARANDO o lote antes de abri-la.")
                break
        try:
            driver.get(ISHIFT_URL)  # reseta pra 'My Home' antes de cada CR
            go_to_display_change_requests(driver)
            open_cr(driver, cr_number)
            if cr_esta_travada(driver):
                msg = (f"[{cr_number}] CR travada ('locked by') ao abrir - nao da pra "
                       f"editar agora (CLAUDE.md gotcha 11). Reprocessar depois.")
                if PARAR_LOTE_NO_ERRO:
                    log.error(msg + " PARANDO o lote.")
                    results["falha"].append(cr_number)
                    break
                log.warning(msg + " Pulando.")
                results["falha"].append(cr_number)
                continue

            # SESSAO B (gotchas 15 e 19): nao insere. Le do servidor, valida os
            # 64 Details, volta a raiz, SALVA (os Details deixam alteracao
            # pendente) e so com esse Save CONFIRMADO clica Submit - tudo na
            # mesma sessao do navegador, com o atestado preso a ela.
            if modo == "conferir_e_submeter":
                _fase_submit = {"atual": "inicio da conferencia"}

                def _prog_submit(ev, _f=_fase_submit, _cr=cr_number):
                    _f["atual"] = f"{ev.get('code')}: {ev.get('fase')}"
                    if ev.get("i"):
                        log.info(
                            f"[{_cr}] conferencia {ev['i']}/{ev['n']} - {ev.get('code')}")

                try:
                    atestado = conferir_impostos_no_servidor(
                        driver, cr_number, codes=TAX_CODES,
                        detalhes_codes=None, on_progresso=_prog_submit)
                except Exception:
                    log.error(
                        f"[{cr_number}] conferencia parou na fase "
                        f"{_fase_submit['atual']!r}. Save e Submit NAO foram clicados.")
                    raise
                try:
                    atestado = salvar_conferida_para_submit(driver, cr_number, atestado)
                except AntesDoClique:
                    raise                  # recusou antes do clique: nada gravado
                except Exception:
                    # o Save foi clicado; salvar_conferida_para_submit ja gravou o
                    # veredito no atestado se chegou a ter um
                    estado = ("salva_nao_submetida"
                              if atestado.get("save_veredito") == "confirmado"
                              else "persistencia_incerta")
                    log.exception(f"[{cr_number}] {estado}: o Save foi clicado e algo "
                                  f"falhou depois. NAO repita esta CR; confira no SAP.")
                    results["ressalva"].append((cr_number, estado, {"atestado": atestado}))
                    break
                if atestado.get("save_veredito") != "confirmado":
                    # Codex, 36a entrada: ressalva no Save NUNCA autoriza Submit.
                    # A regra do alerta duplicado sai do HTML que isto registrou.
                    estado = "conferida_salva_com_ressalva"
                    log.warning(f"[{cr_number}] {estado} - Submit NAO foi clicado.")
                    results["ressalva"].append((cr_number, estado, {"atestado": atestado}))
                    if PARAR_LOTE_NO_ERRO:
                        log.warning(
                            "PARANDO o lote: o Save desta CR ficou com ressalva. NAO a "
                            "repita - o dado provavelmente esta gravado. Navegador fica "
                            "aberto pra inspecao.")
                        break
                    continue
                if not _submeter_no_lote(driver, cr_number, atestado, results):
                    break
                continue

            # FLUXO UNICO (PROPOSTA-FLUXO-UNICO.md): prova a tabela vazia, insere
            # os 64, confere os Details (amostra ou todos, DETAILS_NO_FLUXO_UNICO), leitura integral, Save CONFIRMADO e
            # UM clique em Submit, tudo preso ao atestado desta CR e sessao.
            if modo == "inserir_salvar_submeter":
                _fase_fu = {"atual": "inicio"}

                def _prog_fu(ev, _f=_fase_fu, _cr=cr_number):
                    _f["atual"] = f"{ev.get('code')}: {ev.get('fase')}"
                    if ev.get("i"):
                        log.info(f"[{_cr}] {ev['i']}/{ev['n']} - {ev.get('code')} "
                                 f"({ev.get('fase')})")

                navigate_to_tax_section(driver)
                try:
                    atestado = iniciar_atestado_insercao(driver, cr_number)
                    resumo = insert_all_taxes(driver, cr_number, verificar_profundo_1a=False,
                                              on_progresso=_prog_fu)
                    atestado = atestar_insercao_para_save(
                        driver, cr_number, atestado, resumo, on_progresso=_prog_fu)
                except Exception:
                    log.error(
                        f"[{cr_number}] fluxo unico parou ANTES do Save, na fase "
                        f"{_fase_fu['atual']!r}: nada gravado. Confirmados ate aqui: "
                        f"{inseridos_nesta_execucao(cr_number)}")
                    raise
                try:
                    atestado = salvar_insercao_para_submit(driver, cr_number, atestado)
                except Exception:
                    if atestado.get("fase") in ("insercao_atestada",
                                                "save_recusado_sem_clique"):
                        raise          # recusou antes do clique: nada gravado
                    # o Save foi tentado ou clicado: nao e' falha comum de CR travada
                    estado = atestado.get("fase") or "persistencia_incerta"
                    log.exception(f"[{cr_number}] {estado}: o Save foi tentado/clicado. "
                                  f"NAO repita esta CR; confira no SAP.")
                    results["ressalva"].append((cr_number, estado, {"atestado": atestado}))
                    break
                if atestado.get("save_veredito") != "confirmado":
                    estado = "inserida_salva_com_ressalva"
                    log.warning(f"[{cr_number}] {estado} - Submit NAO foi clicado.")
                    results["ressalva"].append((cr_number, estado, {"atestado": atestado}))
                    log.warning("PARANDO o lote: o Save desta CR ficou com ressalva. NAO a "
                                "repita - o dado provavelmente esta gravado.")
                    break
                if not _submeter_no_lote(driver, cr_number, atestado, results):
                    break
                continue

            navigate_to_tax_section(driver)
            # callback de progresso: sem isto, uma falha no codigo 40 de 64 nao
            # diz em que FASE parou nem o que ja tinha entrado. Aqui vai so pro
            # log (o harness de teste usa o mesmo callback pra screenshots).
            _fase = {"atual": "inicio"}

            def _prog_main(ev, _f=_fase, _cr=cr_number):
                _f["atual"] = f"{ev.get('code')}: {ev.get('fase')}"
                if ev.get("i"):
                    log.info(f"[{_cr}] {ev['i']}/{ev['n']} - {ev.get('code')}")

            try:
                resumo = insert_all_taxes(driver, cr_number, on_progresso=_prog_main)
            except Exception:
                log.error(f"[{cr_number}] parou na fase: {_fase['atual']!r}. "
                          f"Confirmados ate aqui: {inseridos_nesta_execucao(cr_number)}")
                raise
            if SALVAR and resumo.get("pulados"):
                # 9a revisao do Codex: com codigos ja presentes, insert_all_taxes
                # pula com checagem leve (Type + Description). Numa rodada que
                # PERSISTE, isso significaria salvar/submeter sem ter aberto um
                # "Details" sequer pra conferir Code e checkbox das linhas que
                # ja estavam la.
                raise RuntimeError(
                    f"[{cr_number}] {len(resumo['pulados'])} codigo(s) ja estavam na tabela "
                    f"({resumo['pulados']}) e esta rodada SALVA. Nao salvo em cima de linha "
                    f"que nao foi conferida "
                    f"a fundo nesta execucao. Rode antes sem persistir usando "
                    f"reexec='resume_strict' (abre o Details de cada uma), ou limpe a CR.")
            vereditos = finalize_cr(driver, cr_number, salvar=SALVAR, submeter=False) or {}
            # Terceiro estado (12a revisao do Codex): ha DOIS eixos - "o dado
            # persistiu?" e "a automacao terminou sem ressalvas?". A sessao A de
            # 23/09 foi persistiu=True, execucao_ok=False, e o relato so tinha
            # OK/Falha: uma CR que GRAVOU saiu como "Falha: 1", o que convida a
            # repetir - a pior leitura possivel.
            com_ressalva = [v for v in vereditos.values()
                            if isinstance(v, dict) and v.get("veredito") == "ressalva"]
            if com_ressalva:
                estado = "salva_com_ressalva"
                log.warning(f"[{cr_number}] {estado} - {resumo}")
                results["ressalva"].append((cr_number, estado, resumo))
                if PARAR_LOTE_NO_ERRO:
                    log.warning(
                        "PARANDO o lote: uma CR ficou com ressalva. Ela NAO deve ser "
                        "repetida - o dado provavelmente esta gravado. Confira antes de "
                        "seguir. Navegador fica aberto pra inspecao.")
                    break
                continue
            estado = "salva" if SALVAR else "validada_sem_persistir"
            log.info(f"[{cr_number}] {estado} - {resumo}")
            results["ok"].append((cr_number, estado, resumo))
        except Exception as e:
            log.exception(f"[{cr_number}] Processamento interrompido")
            try:
                driver.save_screenshot(os.path.join(
                    PRINTS_DIR, f"falha_{cr_number}_{datetime.now():%Y%m%d_%H%M%S}.png"))
            except Exception:
                pass
            results["falha"].append(cr_number)
            if isinstance(e, SapIndisponivel):
                # gotcha 26: com prova positiva de indisponibilidade nao ha base
                # para abrir a proxima CR, qualquer que seja PARAR_LOTE_NO_ERRO.
                results.setdefault("sap_indisponivel", []).append((cr_number, e.categoria))
                log.error(f"PARANDO o lote: sinal de indisponibilidade ({e.categoria}; "
                          f"detalhe acima). A proxima CR NAO sera aberta.")
                break
            if PARAR_LOTE_NO_ERRO:
                log.error("PARANDO o lote no 1o erro (PARAR_LOTE_NO_ERRO=True). "
                          "Navegador fica aberto pra inspecao.")
                break

    log.info("=" * 60)
    log.info(f"Concluido. OK: {len(results['ok'])} | Com ressalva: "
             f"{len(results['ressalva'])} | Falha: {len(results['falha'])}")
    for cr, estado, resumo in results["ok"]:
        log.info(f"  {cr}: {estado}")
        log.debug(f"  {cr}: {estado} {resumo}")    # o dicionario so no arquivo
    # CR com ressalva foi PROCESSADA (o dado provavelmente gravou) - nao pode
    # entrar em "nao processadas" nem em "falha".
    processadas = [x[0] for x in results["ok"]] + [x[0] for x in results["ressalva"]]
    nao_processadas = [c for c in cr_list
                       if c not in processadas and c not in results["falha"]]
    if results["ressalva"]:
        for cr, estado, resumo in results["ressalva"]:
            log.warning(f"  {cr}: {estado}")
            log.debug(f"  {cr}: {estado} {resumo}")
        log.warning(
            "Nas CRs acima uma acao de persistencia JA FOI CLICADA, mas o resultado ficou "
            "com ressalva. NAO REPITA essas CRs: abra cada uma no SAP, confira o estado "
            "real e siga a partir do que voce vir. Uma ressalva nunca autoriza outra acao.")
    if results["falha"]:
        log.info(f"CRs com falha: {results['falha']}")
        # Este aviso saiu da tela de abertura (era ruido pra quem so quer rodar)
        # e passou a aparecer AQUI, que e' onde ele deixa de ser teoria: a CR
        # falhou depois de o script ter aberto secoes nela, entao ela esta
        # travada no servidor. Sem isto, a reacao natural e' tentar de novo na
        # hora - e a 2a tentativa falha pelo lock, escondendo o erro original.
        log.warning(
            "As CRs acima ficaram TRAVADAS no SAP: so o proprio SAP libera, e isso "
            "costuma levar de algumas horas ate cerca de um dia. Tentar de novo agora "
            "provavelmente falha de novo, por causa da trava e nao do problema original. "
            "Guarde o arquivo de log e os prints antes de repetir.")
    if nao_processadas:
        log.warning(f"CRs NAO processadas (o lote parou antes): {nao_processadas}")
    log.info(f"Log completo salvo em: {LOG_FILE}")
    # o main() pede o ENTER pra fechar o navegador logo depois deste quadro
    linhas = _mensagem_final(results, nao_processadas)
    print("\n" + "\n".join(linhas), flush=True)
    log.debug("mensagem final: " + " | ".join(linhas[1:-1]))

    # exit code != 0 quando alguma CR falhou OU ficou pra tras - o .bat/qualquer
    # wrapper precisa distinguir "rodou tudo" de "parou no meio" (7a revisao).
    # 3 = ressalva: NAO e' sucesso (nao autoriza o passo seguinte) e NAO e'
    # falha operacional (o dado provavelmente gravou, e repetir seria errado).
    # Codigo proprio pra quem automatiza em volta nao tratar os dois igual
    # (12a revisao do Codex).
    # A ressalva vem ANTES (QA de 06/10): ela para o lote, entao numa lista de
    # varias CRs sempre sobram nao processadas - e o 1 escondia o "nao repita"
    # do .bat justamente no uso normal da usuaria (~28 CRs por vez).
    if results["ressalva"]:
        sys.exit(3)
    if results["falha"] or nao_processadas:
        sys.exit(1)


if __name__ == "__main__":
    main()
