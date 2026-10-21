"""
Teste do loop de insercao de codigos de imposto na secao "ERP Vendor Company
Code: Extended Withholding Tax Types" - em duas fases, controladas por MODO:

  MODO = "diagnostico"  (default)
    NAO escreve NADA. Faz login -> abre a CR -> confirma -> navega ate a secao
    de impostos -> DUMPA a estrutura real da tabela (cabecalhos, indices de
    coluna, HTML, linhas) e dos botoes New/Details -> encerra. Serve pra firmar
    o parser de `auto.ler_linhas_impostos` ANTES de confiar nele pra autorizar
    escrita (o DOM da <tr> ainda nao foi capturado - so ha PNGs de 09/09).

  MODO = "escrita"
    Tudo do diagnostico + preflight de idempotencia + `auto.insert_all_taxes`
    com N codigos de LISTA_TESTE (verificacao em 3 camadas por codigo, dentro do
    modulo) + `auto.finalize_cr(salvar=False, submeter=False)` (os 2 "Done" com
    assertions) + dump das acoes de linha. PARA antes do Save.
    NUNCA Save, NUNCA Submit.

DELEGACAO (2026-09-11): a insercao, a verificacao em 3 camadas, as acoes de
linha e o finalize sao os do MODULO (`auto.*`) - este arquivo nao tem mais copia
local deles. Motivo: as copias ja tinham divergido (o modulo ganhou o re-confirm
do checkbox apos o round-trip do WDA e o delta exato de multiset; a copia daqui
nao). Assim o teste exercita EXATAMENTE o codigo que vai pra producao.

Desenho + revisao fechados em 2026-09-10 entre Claude e Codex (ver
QUADRO-CLAUDE-CODEX.md). Pontos de seguranca:
  - TRAVA DURA: so roda com AMBIENTE == "pre_producao".
  - CONFIRMACAO ("CONFIRMAR") vem LOGO APOS abrir a CR e checar o lock - ANTES
    de qualquer navegacao/Details, porque navegar tambem pode travar a CR.
  - MODO "escrita": aborta na hora se a CR vier travada. MODO "diagnostico":
    segue mesmo travada (mapear estrutura numa CR travada e' seguro - gotcha 12).
  - Aborta se "ERP Vendors" ou "Company Codes" tiver != 1 linha com "Details"
    (a automacao clica sempre a 1a linha; regra pra multi-linha e' pendente com
    a usuaria). Contagem ANCORADA na tabela da secao, nao "qualquer Details".
  - Preflight usa `auto.ler_linhas_impostos` (fail closed): so insere codigos
    provados ausentes; se o parser cair no fallback heuristico, levanta e para.
  - QUALQUER falha -> screenshot, log de estado, NAVEGADOR FICA ABERTO, exit!=0.
  - finalize: 2x "Done" com assertions de que a tela mudou. Sem Save/Submit.
  - CLEANUP: sem Delete automatico. So dumpa a <tr> de cada linha (ANTES do
    finalize, com a tabela ainda aberta). Limpeza e' manual - os rascunhos
    pre-prod nao foram salvos e, pelo observado 2x (09/09: ~3h; 26->27/08:
    ~25h), somem quando o lock da CR expira. NAO e' garantia de cleanup.
"""

import os
import sys
import time
import traceback
from datetime import datetime

import sap_ishift_tax_automation as auto

# ==================================================================
# CONFIG - edite aqui
# ==================================================================
MODO = "escrita"                      # "diagnostico" (sem escrita) | "escrita"
CR_TESTE = "90000001"
# Subconjunto de auto.TAX_CODES; o preflight confirma que estao ausentes.
# Historico: T1/T2/T3 (N=3) validado em 2026-09-10; 10 codigos (T0-T6,W0-W2)
# validados em 2026-09-14 com 12,4s/codigo e sem degradacao. Agora a lista
# INTEIRA - que e' o caso de uso real.
LISTA_TESTE = list(auto.TAX_CODES)
N = 64                                # quantos de LISTA_TESTE inserir (1 <= N <= len(LISTA_TESTE))
TESTAR_FINALIZE_ATE_2_DONE = True     # so em MODO "escrita"; D1 do operador: para ANTES do Save
VERIFICACAO_PROFUNDA_1A_LINHA = True  # camada 3 (abre "Details" da 1a linha inserida)
PAUSAR_ENTRE_PASSOS = False           # False = sem ENTER a cada codigo (obrigatorio sem TTY)

MAX_ESPERA_LOGIN_S = 480
INTERVALO_POLL_S = 4

# Quando nao ha terminal interativo (rodando via ferramenta), o input() de
# inspecao no fim levanta EOFError e o navegador fecharia na hora. Nesta rodada
# isso sai caro: inserir codigos TRAVA a CR por horas (gotcha 7/11), entao dado
# de tela que a gente deixar passar so volta na proxima janela. Aqui ele fica
# aberto ate este tempo OU ate alguem criar o arquivo-sinal abaixo.
ESPERA_MAX_INSPECAO_S = 900

# passo-a-passo (auto.pause entre cada acao) so no MODO="escrita" - decisao Codex
# 2026-09-10: escrita e' que precisa de supervisao acao-a-acao. O diagnostico so
# navega e le a tela (nao escreve nada), entao roda direto. A confirmacao
# "CONFIRMAR" antes de navegar continua valendo nos dois modos.
auto.STEP_CONFIRM = (MODO == "escrita") and PAUSAR_ENTRE_PASSOS

_falhou = False            # vira True em qualquer etapa que falhar -> exit code != 0
_estado = {"code": None, "fase": "inicio"}   # onde o fluxo esta, pro log de falha


class AbortarTeste(Exception):
    """Aborto controlado (pre-condicao nao satisfeita) - nao e' bug do fluxo."""


# ==================================================================
# helpers locais
# ==================================================================
def _screenshot(driver, nome):
    arq = os.path.join(auto.PRINTS_DIR, f"loop_{nome}_{datetime.now():%Y%m%d_%H%M%S}.png")
    try:
        driver.save_screenshot(arq)
        print(f"  [screenshot] {arq}", flush=True)
    except Exception as e:
        print(f"  [screenshot] FALHOU ({type(e).__name__}: {e})", flush=True)
    return arq


def _esperar_tela(driver, timeout=20):
    """Espera a tela WDA estabilizar; so engole TimeoutException (Codex)."""
    try:
        auto.wait_for_wda_ready(driver, timeout=timeout)
    except auto.TimeoutException:
        time.sleep(2)


def _texto_tela(driver):
    try:
        return auto._safe_execute_script(
            driver, "return (document.body.innerText || '').replace(/\\s+/g,' ').trim();"
        ) or ""
    except Exception:
        return ""


def _titulo_tela(driver):
    try:
        return auto._safe_execute_script(
            driver,
            "return (document.title || '') + ' :: ' + (document.body.innerText || '')"
            ".replace(/\\s+/g,' ').trim().slice(0, 160);",
        )
    except Exception:
        return "(nao consegui ler a tela)"


def _validar_config():
    if auto.AMBIENTE != "pre_producao":
        raise SystemExit(f"TRAVA: AMBIENTE = {auto.AMBIENTE!r}. Este teste SO roda em "
                         f"'pre_producao'. Abortando sem abrir o navegador.")
    if MODO not in ("diagnostico", "escrita"):
        raise SystemExit(f"CONFIG: MODO invalido {MODO!r} (use 'diagnostico' ou 'escrita').")
    if not (1 <= N <= len(LISTA_TESTE)):
        raise SystemExit(f"CONFIG: N={N} fora de 1..{len(LISTA_TESTE)}.")
    if len(set(LISTA_TESTE)) != len(LISTA_TESTE):
        raise SystemExit(f"CONFIG: LISTA_TESTE tem duplicatas: {LISTA_TESTE}.")
    fora = [c for c in LISTA_TESTE if c not in auto.TAX_CODES]
    if fora:
        raise SystemExit(f"CONFIG: {fora} nao estao em auto.TAX_CODES (nao sao codigos reais).")


def _hook_secao(driver, nome_secao):
    """hook de navigate_to_tax_section. Sempre loga a estrutura da secao.
    MODO='escrita': aborta se != 1 acao 'Details'. MODO='diagnostico': so
    loga e deixa a navegacao seguir (pra dump completo em 1 rodada)."""
    info = auto.dump_secao(driver, nome_secao)   # mesmo JS do modulo
    if not isinstance(info, dict) or info.get("erro"):
        _screenshot(driver, f"erro_secao_{nome_secao.replace(' ', '_')}")
        msg = f"secao '{nome_secao}': {(info or {}).get('erro', info)!r}"
        if MODO == "escrita":
            raise AbortarTeste(msg + " - abortando sem escrever.")
        print(f"  [dump] {msg}", flush=True)
        return

    n = info.get("n_details")
    print(f"\n  [secao {nome_secao!r}] {n} acao(oes) 'Details' | cabecalhos={info.get('cabecalhos')} "
          f"| tabela @ {info.get('tabela_box')}", flush=True)
    for d in info.get("detalhes", []):
        print(f"      Details id={d['id']!r} <{d['tag']}> @ {d['box']} | linha={d['linha_txt']!r}", flush=True)
    print(f"  --- {nome_secao} tabela_html (truncado) ---\n{info.get('tabela_html')}\n", flush=True)
    _screenshot(driver, f"secao_{nome_secao.replace(' ', '_')}")

    if MODO == "escrita" and n != 1:
        # Redundante de proposito: em escrita o clicar_detalhe_secao do modulo
        # ja recusa != 1. Este check adianta a mensagem (com screenshot da secao
        # ja tirado acima) antes de o modulo levantar a dele.
        raise AbortarTeste(
            f"secao '{nome_secao}' tem {n} acoes 'Details' (esperado 1). D4 "
            f"(negocio, 11/09): o fornecedor fica em 1 company code, o da empresa - "
            f"entao != 1 e' situacao nao prevista. Abortando sem escrever nada."
        )


# ==================================================================
# MODO diagnostico
# ==================================================================
def _dump_diagnostico(driver):
    """Dumpa a estrutura real e SINALIZA se ainda nao e' seguro promover pra
    escrita: qualquer problema (sem tabela/cabecalho, mapa de colunas invalido,
    linha em fallback heuristico) -> AbortarTeste (exit != 0)."""
    print("\n================ DUMP DIAGNOSTICO (sem escrita) ================", flush=True)
    _screenshot(driver, "diag_00_secao_impostos")
    problemas = []

    print("\n--- auto.ler_impostos_bruto() ---", flush=True)
    try:
        bruto = auto.ler_impostos_bruto(driver)
    except Exception as e:
        print(f"  FALHOU: {type(e).__name__}: {e}", flush=True)
        bruto = None

    if not isinstance(bruto, dict):
        problemas.append(f"ler_impostos_bruto retornou {bruto!r} (esperava dict)")
    elif bruto.get("erro"):
        problemas.append(f"JS: {bruto['erro']}")
    else:
        it, idd = bruto.get("idxType", -1), bruto.get("idxDesc", -1)
        print(f"  cabecalhos      : {bruto.get('cabecalhos')}", flush=True)
        print(f"  idxType/idxDesc : {it} / {idd}", flush=True)
        print(f"  vazio_confirmado: {bruto.get('vazio_confirmado')}", flush=True)
        linhas = bruto.get("linhas") or []
        print(f"  linhas          : {len(linhas)}", flush=True)
        for i, ln in enumerate(linhas, 1):
            print(f"    [{i}] metodo={ln.get('metodo')!r} type={ln.get('type')!r} "
                  f"desc={ln.get('description')!r} celulas={ln.get('celulas')}", flush=True)
        print("\n  --- tabela_html (truncado) ---", flush=True)
        print(bruto.get("tabela_html") or "(vazio)", flush=True)

        if not (isinstance(it, int) and isinstance(idd, int) and it >= 0 and idd >= 0 and it != idd):
            problemas.append(f"mapa de colunas invalido (idxType={it}, idxDesc={idd}) - "
                             f"ajustar o mapeamento em _LER_LINHAS_IMPOSTOS_JS")
        if not linhas and not bruto.get("vazio_confirmado"):
            problemas.append("achei o cabecalho mas 0 linhas de dados E a tela nao mostra "
                             "'table does not contain any data' - ambiguo, parser nao confiavel "
                             "(mesma trava que ler_linhas_impostos aplica)")
        for ln in linhas:
            if ln.get("metodo") != "header-index":
                problemas.append(f"linha caiu no fallback heuristico: {ln.get('raw')!r}")

    print("\n--- candidatos 'New' (find_clickables_by_text) ---", flush=True)
    try:
        for i, el in enumerate(auto.find_clickables_by_text(driver, "New")[:10], 1):
            d = auto._bounding_info(driver, el)
            eid = auto._safe_execute_script(driver, "return arguments[0].id || '';", el)
            print(f"    [{i}] id={eid!r} pos=({round(d['left'])},{round(d['top'])}) "
                  f"{round(d['width'])}x{round(d['height'])}", flush=True)
    except Exception as e:
        print(f"  FALHOU: {type(e).__name__}: {e}", flush=True)

    if problemas:
        raise AbortarTeste(
            "diagnostico revelou problemas que IMPEDEM MODO='escrita' "
            "(ajustar o parser e rodar o diagnostico de novo):\n  - " + "\n  - ".join(problemas)
        )
    print("\n===== DIAGNOSTICO OK - parser da tabela validado; seguro promover pra escrita =====", flush=True)


# ==================================================================
# MODO escrita
# ==================================================================
def _preflight_escolher_codigos(driver):
    """
    Nesta 1a rodada de escrita EXIGE a tabela comprovadamente VAZIA
    (vazio_confirmado=True E 0 linhas) - o mapeamento de coluna vs. linha de
    dados real ainda nao foi provado, entao o preflight nao consegue garantir
    que leu TODAS as linhas de uma tabela populada (Codex 4a revisao
    2026-09-10). Com a tabela vazia, todos os LISTA_TESTE estao ausentes.
    """
    print("\n--- PREFLIGHT: exigindo tabela de impostos VAZIA (1a rodada) ---", flush=True)
    bruto = auto.ler_impostos_bruto(driver)
    if not isinstance(bruto, dict) or bruto.get("erro"):
        raise AbortarTeste(f"preflight: ler_impostos_bruto falhou: {(bruto or {}).get('erro', bruto)!r}")
    it, idd = bruto.get("idxType", -1), bruto.get("idxDesc", -1)
    linhas = bruto.get("linhas") or []
    print(f"  cabecalhos={bruto.get('cabecalhos')} idxType/idxDesc={it}/{idd} "
          f"vazio_confirmado={bruto.get('vazio_confirmado')} linhas={len(linhas)}", flush=True)
    if not (isinstance(it, int) and isinstance(idd, int) and it >= 0 and idd >= 0 and it != idd):
        raise AbortarTeste(f"preflight: mapa de colunas invalido (idxType={it}, idxDesc={idd}).")
    if not bruto.get("vazio_confirmado") or linhas:
        raise AbortarTeste(
            f"preflight: a tabela de impostos NAO esta comprovadamente vazia "
            f"(vazio_confirmado={bruto.get('vazio_confirmado')}, {len(linhas)} linha(s)). "
            f"Esta 1a rodada exige tabela vazia - o parser de linha ainda nao foi "
            f"validado contra dados reais. Se ha linhas de rodadas anteriores, "
            f"esperar o lock expirar (elas somem) ou limpar manualmente. Abortando."
        )
    # sanity: ler_linhas_impostos (estrito) tem que concordar que esta vazia
    if auto.ler_linhas_impostos(driver):
        raise AbortarTeste("preflight: ler_linhas_impostos retornou linhas numa tabela "
                           "supostamente vazia - inconsistencia, abortando.")
    escolhidos = LISTA_TESTE[:N]
    print(f"  tabela vazia confirmada. Vou inserir: {escolhidos}", flush=True)
    return escolhidos


def _dump_acoes_linhas(driver, codigos):
    """ANTES do finalize (tabela ainda aberta): dumpa as acoes da <tr> de cada
    codigo, pra mapear o Delete de linha numa proxima rodada."""
    print("\n--- dump das acoes de linha (pra mapear Delete depois) ---", flush=True)
    auto.scroll_to_text(driver, "Extended Withholding Tax Types", tamanho_max=(500, 100))
    for code in codigos:
        try:
            r = auto._safe_execute_script(driver, auto._JS_DUMP_ACOES_LINHA, code)
            if isinstance(r, dict) and r.get("acoes") is not None:
                print(f"  <tr> {code!r} (raw={r.get('raw')!r}):", flush=True)
                for a in r["acoes"]:
                    print(f"      {a}", flush=True)
            else:
                print(f"  <tr> {code!r}: {(r or {}).get('erro', r)!r}", flush=True)
        except Exception as e:
            print(f"  dump da linha {code} falhou: {type(e).__name__}: {e}", flush=True)


# Fases em que vale um screenshot. O resto so atualiza o estado (screenshot a
# cada fase deixaria a rodada dos 64 lenta e a pasta prints/ inutil).
_FASES_COM_PRINT = {
    "preencher Type/Code/checkbox": "01_subtela_preenchida",
    "Done da linha - retornou": "02_apos_done",
    "camada 3 (Details)": "03_camada3",
}


def _on_progresso(driver, ev):
    """Callback passado pro auto.insert_all_taxes. Recupera o que a delegacao
    tinha custado: fase corrente pro post-mortem e prints por passo - sem
    reintroduzir copia da logica de insercao."""
    code, fase = ev.get("code"), ev.get("fase")
    _estado["code"] = code
    _estado["fase"] = fase
    if ev.get("i"):
        print(f"\n=== {code}: {ev['i']}/{ev['n']} ===", flush=True)
    elif fase == "confirmado":
        print(f"  {code} confirmado em {ev.get('segundos')}s", flush=True)
    else:
        print(f"  [{code}] {fase}", flush=True)
    nome = _FASES_COM_PRINT.get(fase)
    if nome:
        _screenshot(driver, f"{code}_{nome}")
    # 1a linha commitada: dump bruto da tabela JA POPULADA. E' o unico momento
    # barato de capturar isso, e e' o que falta pra virar fixture do teste
    # offline (hoje ele usa DOM sintetico).
    if fase == "Done da linha - retornou" and not _estado.get("dumpou_1a"):
        _estado["dumpou_1a"] = True
        print("\n  --- DUMP da tabela com a 1a linha real ---", flush=True)
        try:
            print("  ", auto.ler_impostos_bruto(driver), flush=True)
        except Exception as e:
            print(f"  dump bruto falhou: {type(e).__name__}: {e}", flush=True)
        # HTML COMPLETO em arquivo, pra virar fixture do teste offline (o dump
        # acima e' truncado em 4000 chars e nem chega nas <tr> de dados).
        try:
            print("  HTML real salvo em:", auto.capturar_html_tabela(driver), flush=True)
        except Exception as e:
            print(f"  captura do HTML falhou: {type(e).__name__}: {e}", flush=True)


def _on_checkpoint_finalize(driver, ev):
    _estado["fase"] = f"finalize: {ev.get('fase')}"
    print(f"  [finalize] {ev.get('fase')}", flush=True)
    _screenshot(driver, f"finalize_0{ev.get('passo')}_done")


def _finalize_2_done(driver):
    """Delega pro modulo: `auto.finalize_cr` faz os 2 "Done" com as mesmas
    assertions (negativa: saiu de 'ERP Vendor Company Code:'/'ERP Vendor:';
    positiva: marcador da CR + um botao de raiz) e PARA antes do Save.
    salvar=False/submeter=False sao explicitos de proposito - este teste nunca
    persiste."""
    print("\n--- finalize: auto.finalize_cr (2x 'Done'; PARA antes do Save) ---", flush=True)
    auto.finalize_cr(driver, CR_TESTE, salvar=False, submeter=False,
                     on_checkpoint=lambda ev: _on_checkpoint_finalize(driver, ev))
    print(f"  tela apos finalize: {_titulo_tela(driver)!r}", flush=True)

    # Dump READ-ONLY dos botoes da tela raiz (item 6.3 da 8a revisao do Codex).
    # Sem clicar em nada: e' isto que responde, NESTA janela, o dedupe dos nos,
    # qual marcador indica "desabilitado" no WDA e se o numero da CR aparece
    # mesmo na tela raiz - as tres duvidas que hoje seguram o Save.
    print("\n--- dump READ-ONLY dos botoes Save/Submit/Withdraw (sem clicar) ---", flush=True)
    try:
        print("  ", auto.dump_acoes_raiz(driver, CR_TESTE), flush=True)
    except Exception as e:
        print(f"  dump da raiz falhou: {type(e).__name__}: {e}", flush=True)
    _screenshot(driver, "finalize_tela_raiz")
    print("  PARANDO AQUI - sem Save, sem Submit.", flush=True)


# ==================================================================
# main
# ==================================================================
def main():
    global _falhou
    _validar_config()

    # Log em ARQUIVO, nao so console: a janela de CR destravada e' cara e a
    # saida do terminal se perde (8a revisao do Codex, item 6.5).
    auto._ativar_log_em_arquivo()
    print(f"log desta rodada: {auto.LOG_FILE}", flush=True)
    print(f"MODO={MODO} | ambiente={auto.AMBIENTE} -> {auto.ISHIFT_URL}", flush=True)
    print(f"CR={CR_TESTE} | LISTA_TESTE={LISTA_TESTE} | N={N} | "
          f"finalize_2_done={TESTAR_FINALIZE_ATE_2_DONE}", flush=True)

    codigos_inseridos = []
    driver = None
    try:
        driver = auto.criar_driver_edge()
        driver.maximize_window()
        driver.get(auto.ISHIFT_URL)

        # mesma funcao que o main() usa (promovida pro modulo em 14/09)
        try:
            auto.esperar_login_manual(driver, timeout=MAX_ESPERA_LOGIN_S,
                                      intervalo=INTERVALO_POLL_S)
        except Exception as e:
            raise AbortarTeste(f"espera do login: {e}")

        auto.go_to_display_change_requests(driver)
        auto.open_cr(driver, CR_TESTE)
        print("CR aberta.", flush=True)

        travada = auto.cr_esta_travada(driver)
        if travada:
            _screenshot(driver, "cr_travada")
            if MODO == "escrita":
                raise AbortarTeste(
                    f"CR {CR_TESTE} veio TRAVADA ('locked by') e MODO='escrita'. "
                    f"Abortando - tentar de novo em algumas horas (o lock costuma "
                    f"soltar sozinho, CLAUDE.md gotcha 11). Pra mapear estrutura, "
                    f"rode com MODO='diagnostico'."
                )
            print("  CR travada, mas MODO='diagnostico' - mapear estrutura numa CR "
                  "travada e' seguro (gotcha 12). Seguindo.", flush=True)

        # CONFIRMACAO - ANTES de navegar (navegar/Details tambem pode travar a CR)
        if MODO == "escrita":
            aviso = (f"MODO='escrita': vou navegar ate a secao de impostos e INSERIR "
                     f"{N} codigo(s) ({LISTA_TESTE[:N]}) na CR {CR_TESTE} (PRE-PRODUCAO) "
                     f"via auto.insert_all_taxes. "
                     f"Navegar, abrir 'Details' e 'New' TRAVAM a CR ate o lock expirar. "
                     f"NAO havera Save nem Submit.")
        else:
            aviso = (f"MODO='diagnostico': NAO vou escrever nada. Mas navegar ate a secao "
                     f"e abrir 'Details' pode deixar a CR {CR_TESTE} em lock que solta "
                     f"sozinho em horas.")
        try:
            resp = input(f"\n>>> {aviso}\n>>> Digite CONFIRMAR pra seguir: ").strip()
        except EOFError:
            resp = ""
        if resp != "CONFIRMAR":
            raise AbortarTeste("confirmacao nao recebida (esperava 'CONFIRMAR').")

        # checar_linha_unica segue o MODO, de proposito:
        #   escrita     -> True  = EXATAMENTE o caminho que o main() usa
        #                  (clicar_detalhe_secao: ancora na tabela da secao,
        #                  exige 1 "Details" e clica AQUELE elemento).
        #   diagnostico -> False = precisa seguir mesmo com != 1 pra dumpar a tela.
        #
        # Ate 14/09 isto era False nos dois modos, e o resultado e' que as duas
        # rodadas de 64 codigos validaram `click_visible_candidate` enquanto o
        # main() - o que a usuaria roda de verdade - usa `clicar_detalhe_secao`.
        # Mesma familia do problema das copias locais divergentes: o teste
        # exercitava um caminho e a producao usava outro.
        auto.navigate_to_tax_section(
            driver, hook_antes_do_clique=lambda s: _hook_secao(driver, s),
            checar_linha_unica=(MODO == "escrita"),
        )

        if MODO == "diagnostico":
            _dump_diagnostico(driver)   # levanta AbortarTeste se o parser ainda nao esta seguro
            print("\n=== DIAGNOSTICO OK (nenhuma escrita) - pode revisar o log e promover pra MODO='escrita' ===", flush=True)
        else:
            _estado["fase"] = "preflight"
            escolhidos = _preflight_escolher_codigos(driver)

            # A INSERCAO E' A DO MODULO - este teste valida o codigo que vai pra
            # producao, nao uma copia parecida. insert_all_taxes faz por codigo:
            # camada 1 (campos + checkbox re-confirmado apos o round-trip do WDA),
            # camada 2 (delta exato do multiset da tabela) e, no 1o, camada 3
            # (abre o Details e confere Code/checkbox persistidos).
            _estado["fase"] = f"auto.insert_all_taxes ({len(escolhidos)} codigos) - EM ANDAMENTO"
            print(f"\n=== auto.insert_all_taxes: {escolhidos} ===", flush=True)
            resumo = auto.insert_all_taxes(
                driver, CR_TESTE, codes=escolhidos,
                verificar_profundo_1a=VERIFICACAO_PROFUNDA_1A_LINHA,
                on_progresso=lambda ev: _on_progresso(driver, ev),
            )
            codigos_inseridos = list(resumo.get("inseridos") or [])
            _estado["code"] = None
            _estado["fase"] = "insert_all_taxes completo"
            print(f"  resumo: {resumo}", flush=True)
            _screenshot(driver, "apos_insert_all_taxes")

            if sorted(codigos_inseridos) != sorted(escolhidos):
                raise RuntimeError(
                    f"insert_all_taxes devolveu inseridos={codigos_inseridos} mas o preflight "
                    f"escolheu {escolhidos} (tabela estava vazia) - inconsistente.")

            print(f"\nTODOS os {len(escolhidos)} codigos entraram: {codigos_inseridos}", flush=True)

            # HTML da tabela CHEIA - o caso que mais interessa pro teste offline,
            # porque e' onde virtualizacao/paginacao apareceriam se existissem.
            try:
                print("  HTML da tabela cheia:",
                      auto.capturar_html_tabela(driver,
                                                nome=f"tabela_real_{len(codigos_inseridos)}linhas_"
                                                     f"{datetime.now():%Y%m%d_%H%M%S}.html"),
                      flush=True)
            except Exception as e:
                print(f"  captura do HTML cheio falhou: {type(e).__name__}: {e}", flush=True)
            _dump_acoes_linhas(driver, codigos_inseridos)   # ANTES do finalize (Codex)

            if TESTAR_FINALIZE_ATE_2_DONE:
                _finalize_2_done(driver)

            print("\n  Limpeza e' MANUAL. Nada foi salvo (sem Save/Submit). Pelo observado "
                  "2x (09/09 ~3h; 26->27/08 ~25h) os rascunhos pre-prod somem quando o "
                  "lock da CR expira - NAO e' garantia.", flush=True)
            print("\n=== ESCRITA OK ===", flush=True)

    except AbortarTeste as e:
        _falhou = True
        print(f"\n### ABORTADO: {e}", flush=True)
        if driver is not None:
            _screenshot(driver, "abortado")
    except Exception as e:
        _falhou = True
        print(f"\n### FALHA: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        if driver is not None:
            _screenshot(driver, "falha")
            print(f"\n### ESTADO: MODO={MODO} CR={CR_TESTE}", flush=True)
            # insert_all_taxes LEVANTA antes de devolver o resumo, entao
            # codigos_inseridos fica vazio mesmo com linhas commitadas. O
            # registro do modulo sobrevive a excecao - e' ele que vale aqui
            # (8a revisao do Codex, ponto 5).
            try:
                confirmados = auto.inseridos_nesta_execucao(CR_TESTE)
            except Exception:
                confirmados = "(nao consegui ler o registro)"
            print(f"###   codigos confirmados (registro do modulo): {confirmados}", flush=True)
            print(f"###   resumo devolvido (vazio se levantou): {codigos_inseridos}", flush=True)
            print(f"###   em andamento quando falhou: code={_estado['code']!r} fase={_estado['fase']!r}", flush=True)
            print(f"###   tela: {_titulo_tela(driver)!r}", flush=True)
            if _estado["fase"] and "EM ANDAMENTO" in _estado["fase"]:
                print(f"###   ATENCAO: {_estado['code']} pode ter sido COMMITADO na tabela mesmo "
                      f"nao estando na lista acima - conferir na tela.", flush=True)
            print("### RECUPERACAO: nada foi salvo (sem Save/Submit). Se algum codigo entrou, "
                  "a CR fica TRAVADA com esses rascunhos ate o lock expirar (gotcha 11). "
                  "Inspecione a tela ABERTA; pra limpar agora, use Undo/Delete manualmente e "
                  "confira o aviso 'locked by' sumindo antes de fechar.", flush=True)
    finally:
        if driver is not None:
            try:
                input("\n[inspecao] o navegador segue ABERTO. ENTER pra fechar...")
            except EOFError:
                auto.esperar_inspecao(segundos=ESPERA_MAX_INSPECAO_S)
            try:
                driver.quit()
            except Exception:
                pass

    sys.exit(1 if _falhou else 0)


if __name__ == "__main__":
    main()
