"""
ENSAIO da sessao B (gotcha 15) - SEM Submit, SEM Save.

Roda EXATAMENTE a conferencia que o main() usa antes do Submit
(`auto.conferir_impostos_no_servidor`, todos os 64 Details) e para ai. Serve pra
achar bug nesse codigo - que nunca rodou contra o SAP - ANTES da janela do
primeiro Submit de verdade, em vez de durante ela.

Por que um script separado, e nao o main() com SUBMETER=True respondendo "nao"
no checkpoint: aquilo dependeria de ninguem digitar OK por reflexo. Aqui nao
existe caminho ate o Submit - e, por garantia, as funcoes e os JS de clique de
Save/Submit do modulo sao SUBSTITUIDOS por versoes que levantam erro.

O atestado produzido aqui NAO serve pro Submit real: ele e' preso ao
session_id deste navegador. A sessao B de verdade refaz a conferencia inteira.

Custo conhecido: navegar ate a secao de impostos TRAVA a CR (gotcha 7) - depois
deste ensaio a 90000001 fica travada de algumas horas a ~1 dia.

De brinde, captura o HTML REAL da tabela com as 64 linhas (fixture pendente
desde 11/09 - ver capturar_html_tabela) e o dump read-only dos botoes da raiz.
"""

import sys
import time
import traceback
from datetime import datetime

import sap_ishift_tax_automation as auto

# ==================================================================
# CONFIG
# ==================================================================
CR_TESTE = "90000001"
MAX_ESPERA_LOGIN_S = 480
INTERVALO_POLL_S = 4
LIMITE_HTML_TABELA = 800000       # a tabela cheia do WDA e' grande; truncou, avisa no log

auto.STEP_CONFIRM = False         # 64 Details = 64 pausas; o ensaio roda direto


# ==================================================================
# TRAVAS: nada neste processo pode clicar Save nem Submit
# ==================================================================
def _proibido(nome):
    def _f(*args, **kwargs):
        raise RuntimeError(
            f"ENSAIO: '{nome}' foi chamado e e' PROIBIDO neste script - nada pode ser "
            f"salvo nem submetido. Isto e' bug: a conferencia nao deveria chegar aqui.")
    return _f


for _nome in ("submeter_cr_validada_da_raiz", "_clicar_submit_validado_da_raiz",
              "salvar_conferida_para_submit", "salvar_cr_da_raiz", "_clicar_acao_raiz",
              "finalize_cr"):
    setattr(auto, _nome, _proibido(_nome))
# Segunda camada: mesmo que algum caminho chegue ao JS de clique por outra via,
# o proprio script levanta dentro do navegador antes de tocar no botao.
auto._JS_CLICAR_SUBMIT_VALIDADO = "throw new Error('ENSAIO: clique de Submit proibido');"
auto._JS_CLICAR_ACAO_RAIZ = "throw new Error('ENSAIO: clique de Save/Submit proibido');"


class AbortarEnsaio(Exception):
    """Pre-condicao nao satisfeita (CR travada, sem confirmacao) - nao e' bug."""


def _screenshot(driver, nome):
    arq = f"{auto.PRINTS_DIR}/ensaio_b_{nome}_{datetime.now():%Y%m%d_%H%M%S}.png"
    try:
        driver.save_screenshot(arq)
        print(f"  [screenshot] {arq}", flush=True)
    except Exception as e:
        print(f"  [screenshot] FALHOU ({type(e).__name__}: {e})", flush=True)


def main():
    if auto.AMBIENTE != "pre_producao":
        print(f"ENSAIO so roda em pre_producao (AMBIENTE={auto.AMBIENTE!r}). Encerrando.")
        return 2

    auto._ativar_log_em_arquivo()
    print(f"log desta rodada: {auto.LOG_FILE}", flush=True)
    print(f"ENSAIO sessao B (sem Submit) | CR={CR_TESTE} | {auto.ISHIFT_URL}", flush=True)

    fase = {"atual": "inicio"}
    tabela_capturada = {"feito": False}
    driver = None
    ok = False
    t0 = None
    try:
        driver = auto.criar_driver_edge()   # mesma funcao do main(): prompt fica ABERTO
        driver.maximize_window()
        driver.get(auto.ISHIFT_URL)
        try:
            auto.esperar_login_manual(driver, timeout=MAX_ESPERA_LOGIN_S,
                                      intervalo=INTERVALO_POLL_S)
        except Exception as e:
            raise AbortarEnsaio(f"espera do login: {e}")

        auto.go_to_display_change_requests(driver)
        auto.open_cr(driver, CR_TESTE)
        if auto.cr_esta_travada(driver):
            _screenshot(driver, "cr_travada")
            raise AbortarEnsaio(
                f"CR {CR_TESTE} veio TRAVADA ('locked by'). Nada foi clicado. Tentar de "
                f"novo em algumas horas (gotcha 11).")

        # ANTES de navegar: navegar e abrir Details TRAVAM a CR.
        aviso = (f"ENSAIO: vou conferir os 64 Details da CR {CR_TESTE} (PRE-PRODUCAO). "
                 f"NAO ha Save nem Submit neste script. Navegar TRAVA a CR por "
                 f"horas a ~1 dia.")
        try:
            resp = input(f"\n>>> {aviso}\n>>> Digite CONFIRMAR pra seguir: ")
        except EOFError:
            resp = ""
        if auto._normalizar_confirmacao(resp) != "CONFIRMAR":
            raise AbortarEnsaio("confirmacao nao recebida (esperava 'CONFIRMAR'). "
                                "Nada foi clicado.")

        def _progresso(ev):
            fase["atual"] = f"{ev.get('code')}: {ev.get('fase')}"
            if ev.get("i"):
                auto.log.info(f"[{CR_TESTE}] ensaio {ev['i']}/{ev['n']} - {ev.get('code')}")
            # Antes do 1o Details a tabela cheia esta na tela: e' a hora da fixture.
            if ev.get("i") == 1 and not tabela_capturada["feito"]:
                tabela_capturada["feito"] = True
                try:
                    caminho = auto.capturar_html_tabela(
                        driver, limite=LIMITE_HTML_TABELA,
                        nome=f"tabela_real_64linhas_{datetime.now():%Y%m%d_%H%M%S}.html")
                    print(f"  HTML real da tabela cheia: {caminho}", flush=True)
                except Exception as e:
                    print(f"  captura do HTML falhou (segue o ensaio): "
                          f"{type(e).__name__}: {e}", flush=True)

        t0 = time.time()
        atestado = auto.conferir_impostos_no_servidor(
            driver, CR_TESTE, codes=auto.TAX_CODES, detalhes_codes=None,
            on_progresso=_progresso)
        duracao = time.time() - t0

        fase["atual"] = "dump read-only da raiz"
        try:
            auto.log.info(f"[{CR_TESTE}] dump_acoes_raiz: {auto.dump_acoes_raiz(driver, CR_TESTE)!r}")
        except Exception as e:
            print(f"  dump_acoes_raiz falhou (nao afeta o resultado): "
                  f"{type(e).__name__}: {e}", flush=True)
        _screenshot(driver, "raiz_final")

        # Desde o gotcha 19 a conferencia termina com o Save HABILITADO e so
        # autoriza o Save (que aqui e' proibido) - nao mais o Submit direto.
        ok = bool(atestado.get("apto_para_save"))
        print("\n" + "=" * 64, flush=True)
        print(f"  Conferencia: {len(atestado.get('detalhes_validados') or [])}/"
              f"{len(atestado.get('codes_esperados') or [])} Details validados "
              f"em {duracao / 60:.1f} min", flush=True)
        if ok:
            print("  ENSAIO OK - a sessao B chegaria ao Save (e dele ao Submit).", flush=True)
        else:
            print("  ENSAIO: atestado INAPTO - a sessao B NAO chegaria nem ao Save. "
                  "Ver o log.", flush=True)
        print("  Save e Submit NAO foram clicados. A CR fica TRAVADA por horas a ~1 dia.",
              flush=True)
        print("=" * 64, flush=True)

    except AbortarEnsaio as e:
        print(f"\n### ABORTADO: {e}", flush=True)
        if driver is not None:
            _screenshot(driver, "abortado")
    except Exception as e:
        print(f"\n### FALHA na fase {fase['atual']!r}: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        auto.log.exception(f"[{CR_TESTE}] ensaio falhou na fase {fase['atual']!r}")
        if t0 is not None:
            print(f"### depois de {(time.time() - t0) / 60:.1f} min de conferencia", flush=True)
        if driver is not None:
            _screenshot(driver, "falha")
        print("### Submit NAO foi clicado. Se chegou a navegar, a CR ficou TRAVADA.", flush=True)
    finally:
        if driver is not None:
            try:
                input("\nPressione ENTER para fechar o navegador...")
            except EOFError:
                auto.esperar_inspecao()
            try:
                driver.quit()
            except Exception:
                pass
        print(f"Log completo: {auto.LOG_FILE}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
